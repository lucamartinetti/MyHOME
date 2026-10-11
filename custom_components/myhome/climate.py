"""Support for MyHome heating."""

import asyncio

from homeassistant.components.climate import (
    ClimateEntity,
    DOMAIN as PLATFORM,
)
from homeassistant.components.climate.const import (
    FAN_OFF,
    FAN_AUTO,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_MAC,
    UnitOfTemperature,
)

from OWNd.message import (
    OWNCommand,
    OWNHeatingEvent,
    OWNHeatingCommand,
    CLIMATE_MODE_OFF,
    CLIMATE_MODE_HEAT,
    CLIMATE_MODE_COOL,
    CLIMATE_MODE_AUTO,
    MESSAGE_TYPE_MAIN_TEMPERATURE,
    MESSAGE_TYPE_MAIN_HUMIDITY,
    MESSAGE_TYPE_TARGET_TEMPERATURE,
    MESSAGE_TYPE_LOCAL_OFFSET,
    MESSAGE_TYPE_LOCAL_TARGET_TEMPERATURE,
    MESSAGE_TYPE_MODE,
    MESSAGE_TYPE_MODE_TARGET,
    MESSAGE_TYPE_ACTION,
)

from .const import (
    CONF_PLATFORMS,
    CONF_ENTITY,
    CONF_WHO,
    CONF_ZONE,
    CONF_MANUFACTURER,
    CONF_DEVICE_MODEL,
    CONF_HEATING_SUPPORT,
    CONF_COOLING_SUPPORT,
    CONF_FAN_SUPPORT,
    CONF_STANDALONE,
    CONF_CENTRAL,
    DOMAIN,
    LOGGER,
)
from .myhome_device import MyHOMEEntity
from .gateway import MyHOMEGatewayHandler

# Local patch (homeassistant-setup patches/myhome/0001): MyHOMEServer1 software central.
# On this plant the thermoregulation central is a MyHOMEServer1 (the climate block is
# configured under that gateway). It only acts on the zone writes the BTicino app sends,
# dimension 7: `*#4*Z*#7*<season>*1*<TTTT>##` (manual setpoint) and `*#4*Z*#7*<season>*2*<TTTT>##`
# (off = protection at 35.0 cooling / 7.0 heating; without the temperature it is ignored),
# season 2 = cooling, 1 = heating. The stock dimension-14 and
# `*4*303*#Z##` commands are ignored (verified on the bus 2026-10-03). No weekly program
# is used, so AUTO is not offered. The season follows the zones that can heat AND cool
# (they report 0/2xx in summer, 1/1xx in winter); single-season zones offer only OFF
# outside their season.
SEASON_COOL = "2"
SEASON_HEAT = "1"
DEFAULT_RESUME = {SEASON_COOL: 26.0, SEASON_HEAT: 20.0}
PROTECTION = {SEASON_COOL: "0350", SEASON_HEAT: "0070"}


async def async_setup_entry(hass, config_entry, async_add_entities):
    if PLATFORM not in hass.data[DOMAIN][config_entry.data[CONF_MAC]][CONF_PLATFORMS]:
        return True

    _climate_devices = []
    _configured_climate_devices = hass.data[DOMAIN][config_entry.data[CONF_MAC]][
        CONF_PLATFORMS
    ][PLATFORM]

    for _climate_device in _configured_climate_devices.keys():
        _climate_devices.append(
            MyHOMEClimate(
                hass=hass,
                device_id=_climate_device,
                who=_configured_climate_devices[_climate_device][CONF_WHO],
                where=_configured_climate_devices[_climate_device][CONF_ZONE],
                name=_configured_climate_devices[_climate_device][CONF_NAME],
                heating=_configured_climate_devices[_climate_device][
                    CONF_HEATING_SUPPORT
                ],
                cooling=_configured_climate_devices[_climate_device][
                    CONF_COOLING_SUPPORT
                ],
                fan=_configured_climate_devices[_climate_device][CONF_FAN_SUPPORT],
                standalone=_configured_climate_devices[_climate_device][
                    CONF_STANDALONE
                ],
                central=_configured_climate_devices[_climate_device][CONF_CENTRAL],
                manufacturer=_configured_climate_devices[_climate_device][
                    CONF_MANUFACTURER
                ],
                model=_configured_climate_devices[_climate_device][CONF_DEVICE_MODEL],
                gateway=hass.data[DOMAIN][config_entry.data[CONF_MAC]][CONF_ENTITY],
            )
        )

    async_add_entities(_climate_devices)


async def async_unload_entry(hass, config_entry):
    if PLATFORM not in hass.data[DOMAIN][config_entry.data[CONF_MAC]][CONF_PLATFORMS]:
        return True

    _configured_climate_devices = hass.data[DOMAIN][config_entry.data[CONF_MAC]][
        CONF_PLATFORMS
    ][PLATFORM]

    for _climate_device in _configured_climate_devices.keys():
        del hass.data[DOMAIN][config_entry.data[CONF_MAC]][CONF_PLATFORMS][PLATFORM][
            _climate_device
        ]


class MyHOMEClimate(MyHOMEEntity, ClimateEntity):
    def __init__(
        self,
        hass,
        name: str,
        device_id: str,
        who: str,
        where: str,
        heating: bool,
        cooling: bool,
        fan: bool,
        standalone: bool,
        central: bool,
        manufacturer: str,
        model: str,
        gateway: MyHOMEGatewayHandler,
    ):
        super().__init__(
            hass=hass,
            name=name,
            platform=PLATFORM,
            device_id=device_id,
            who=who,
            where=where,
            manufacturer=manufacturer,
            model=model,
            gateway=gateway,
        )

        self._standalone = standalone
        self._central = True if self._where == "#0" else central

        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_precision = 0.1
        self._attr_target_temperature_step = 0.5
        self._attr_min_temp = 5
        self._attr_max_temp = 40

        self._attr_supported_features = 0
        self._attr_hvac_modes = [HVACMode.OFF]
        self._heating = heating
        self._cooling = cooling
        if heating or cooling:
            self._attr_supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE
            if heating:
                self._attr_hvac_modes.append(HVACMode.HEAT)
            if cooling:
                self._attr_hvac_modes.append(HVACMode.COOL)

        self._attr_fan_modes = []
        self._fan = fan
        if fan:
            self._attr_supported_features |= ClimateEntityFeature.FAN_MODE
            self._attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_OFF]

        self._attr_current_temperature = None
        self._attr_current_humidity = None
        self._target_temperature = None
        self._local_offset = 0
        self._local_target_temperature = None

        self._attr_hvac_mode = None
        self._attr_hvac_action = None

        self._attr_fan_mode = None

        self._zone = str(self._where).split("#")[-1]
        self._resume_setpoint = None
        # patches/myhome/0003: zone demand from the central's 4001/4002 frames (None = not seen yet).
        self._demand = None
        self._echo_seen = True

    _plant_season = None  # shared: one thermoregulation plant

    def _season(self):
        return MyHOMEClimate._plant_season

    @property
    def hvac_modes(self):
        modes = [HVACMode.OFF]
        if self._season() == SEASON_COOL and self._cooling:
            modes.append(HVACMode.COOL)
        elif self._season() == SEASON_HEAT and self._heating:
            modes.append(HVACMode.HEAT)
        return modes

    async def _send_frame(self, frame, retries=2):
        message = OWNCommand.parse(frame)
        if message is None or not message.is_valid:
            LOGGER.error("%s Invalid thermoregulation frame `%s`, not sent.", self._gateway_handler.log_id, frame)
            return
        # `*#4*Z*#7*S*M*TTTT##`: M 1 = manual setpoint (on), 2 = protection (off)
        self._expect_off = frame.split("*")[5] == "2"
        self._echo_seen = False
        await self._gateway_handler.send(message)
        if retries:
            # patches/myhome/0003: the .46 central sometimes drops a zone write (seen 2026-10-05: the first command after a
            # reload, and a command sent during the post-start zone re-query burst). An accepted write is echoed within
            # ~1 s by the zone's mode frame in the requested state (off / on); without it in 8 s, send it again.
            self.hass.async_create_task(self._retry_if_no_echo(frame, retries))

    async def _retry_if_no_echo(self, frame, retries):
        await asyncio.sleep(8)
        if not self._echo_seen:
            LOGGER.warning("%s Zone %s did not echo `%s`; sending it again.", self._gateway_handler.log_id, self._zone, frame)
            await self._send_frame(frame, retries=retries - 1)

    async def _send_setpoint(self, temperature):
        season = self._season()
        temperature = min(max(round(temperature * 2) / 2, 5.0), 40.0)
        await self._send_frame(f"*#4*{self._zone}*#7*{season}*1*{int(round(temperature * 10)):04d}##")

    async def async_update(self):
        """Update the entity.

        Only used by the generic entity update service.
        """
        await self._gateway_handler.send_status_request(
            OWNHeatingCommand.status(self._where)
        )

    @property
    def target_temperature(self) -> float:
        if self._attr_hvac_mode == HVACMode.OFF:
            return None
        if self._local_target_temperature is not None:
            return self._local_target_temperature
        else:
            return self._target_temperature

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target hvac mode (dimension-7 zone writes, see the patch note above)."""
        season = self._season()
        if season is None:
            LOGGER.warning("%s Season unknown yet; not changing zone %s.", self._gateway_handler.log_id, self._zone)
            return
        if hvac_mode == HVACMode.OFF:
            await self._send_frame(f"*#4*{self._zone}*#7*{season}*2*{PROTECTION[season]}##")
        elif hvac_mode in self.hvac_modes:
            await self._send_setpoint(self._resume_setpoint or DEFAULT_RESUME[season])
        else:
            LOGGER.warning("%s Zone %s: %s is not available in this season.", self._gateway_handler.log_id, self._zone, hvac_mode)

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature. While the zone is off it is only remembered for the next turn-on."""
        if (temperature := kwargs.get("temperature")) is None:
            return
        temperature -= self._local_offset
        hvac_mode = kwargs.get("hvac_mode", self._attr_hvac_mode)
        if hvac_mode == HVACMode.OFF or self._season() is None:
            self._resume_setpoint = temperature
            return
        await self._send_setpoint(temperature)

    def _active_action(self):
        if self._season() == SEASON_COOL and self._cooling:
            return HVACAction.COOLING
        if self._season() == SEASON_HEAT and self._heating:
            return HVACAction.HEATING
        return HVACAction.COOLING if self._cooling and not self._heating else HVACAction.HEATING

    def _apply_demand(self):
        if self._attr_hvac_mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF
        elif self._demand:
            self._attr_hvac_action = self._active_action()
        elif self._demand is False:
            self._attr_hvac_action = HVACAction.IDLE

    def handle_demand(self, on: bool):
        """Zone demand from the central (patches/myhome/0003): 4001 = calling, 4002 = satisfied."""
        self._demand = on
        self._apply_demand()
        self.async_schedule_update_ha_state()

    def handle_event(self, message: OWNHeatingEvent):
        """Handle an event message."""
        if message.message_type in (MESSAGE_TYPE_MODE, MESSAGE_TYPE_MODE_TARGET) and message.mode is not None:
            if (message.mode == CLIMATE_MODE_OFF) == getattr(self, "_expect_off", None):
                self._echo_seen = True
        if self._heating and self._cooling and message.message_type in (MESSAGE_TYPE_MODE, MESSAGE_TYPE_MODE_TARGET):
            code = str(getattr(message, "_mode", ""))
            if code == "0" or (len(code) >= 3 and code[0] == "2"):
                MyHOMEClimate._plant_season = SEASON_COOL
            elif code == "1" or (len(code) >= 3 and code[0] == "1"):
                MyHOMEClimate._plant_season = SEASON_HEAT
        if message.message_type == MESSAGE_TYPE_MAIN_TEMPERATURE:
            LOGGER.info(
                "%s %s",
                self._gateway_handler.log_id,
                message.human_readable_log,
            )
            self._attr_current_temperature = message.main_temperature
        elif message.message_type == MESSAGE_TYPE_MAIN_HUMIDITY:
            LOGGER.info(
                "%s %s",
                self._gateway_handler.log_id,
                message.human_readable_log,
            )
            self._attr_current_humidity = message.main_humidity
        elif message.message_type == MESSAGE_TYPE_TARGET_TEMPERATURE:
            LOGGER.info(
                "%s %s",
                self._gateway_handler.log_id,
                message.human_readable_log,
            )
            self._target_temperature = message.set_temperature
            self._local_target_temperature = (
                self._target_temperature + self._local_offset
            )
        elif message.message_type == MESSAGE_TYPE_LOCAL_OFFSET:
            LOGGER.info(
                "%s %s",
                self._gateway_handler.log_id,
                message.human_readable_log,
            )
            self._local_offset = message.local_offset
            if self._target_temperature is not None:
                self._local_target_temperature = (
                    self._target_temperature + self._local_offset
                )
        elif message.message_type == MESSAGE_TYPE_LOCAL_TARGET_TEMPERATURE:
            LOGGER.info(
                "%s %s",
                self._gateway_handler.log_id,
                message.human_readable_log,
            )
            self._local_target_temperature = message.local_set_temperature
            self._target_temperature = (
                self._local_target_temperature - self._local_offset
            )
        elif message.message_type == MESSAGE_TYPE_MODE:
            if (
                message.mode == CLIMATE_MODE_AUTO
                and HVACMode.AUTO in self._attr_hvac_modes
            ):
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.AUTO
                if self._attr_hvac_action == HVACAction.OFF:
                    self._attr_hvac_action = HVACAction.IDLE
            elif (
                message.mode == CLIMATE_MODE_COOL
                and HVACMode.COOL in self._attr_hvac_modes
            ):
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.COOL
                if self._attr_hvac_action == HVACAction.OFF:
                    self._attr_hvac_action = HVACAction.IDLE
            elif (
                message.mode == CLIMATE_MODE_HEAT
                and HVACMode.HEAT in self._attr_hvac_modes
            ):
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.HEAT
                if self._attr_hvac_action == HVACAction.OFF:
                    self._attr_hvac_action = HVACAction.IDLE
            elif message.mode == CLIMATE_MODE_OFF:
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.OFF
                self._attr_hvac_action = HVACAction.OFF
        elif message.message_type == MESSAGE_TYPE_MODE_TARGET:
            if (
                message.mode == CLIMATE_MODE_AUTO
                and HVACMode.AUTO in self._attr_hvac_modes
            ):
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.AUTO
                if self._attr_hvac_action == HVACAction.OFF:
                    self._attr_hvac_action = HVACAction.IDLE
            elif (
                message.mode == CLIMATE_MODE_COOL
                and HVACMode.COOL in self._attr_hvac_modes
            ):
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.COOL
                if self._attr_hvac_action == HVACAction.OFF:
                    self._attr_hvac_action = HVACAction.IDLE
            elif (
                message.mode == CLIMATE_MODE_HEAT
                and HVACMode.HEAT in self._attr_hvac_modes
            ):
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.HEAT
                if self._attr_hvac_action == HVACAction.OFF:
                    self._attr_hvac_action = HVACAction.IDLE
            elif message.mode == CLIMATE_MODE_OFF:
                LOGGER.info(
                    "%s %s",
                    self._gateway_handler.log_id,
                    message.human_readable_log,
                )
                self._attr_hvac_mode = HVACMode.OFF
                self._attr_hvac_action = HVACAction.OFF
            self._target_temperature = message.set_temperature
            self._local_target_temperature = (
                self._target_temperature + self._local_offset
            )
        elif message.message_type == MESSAGE_TYPE_ACTION:
            LOGGER.info(
                "%s %s",
                self._gateway_handler.log_id,
                message.human_readable_log,
            )
            if self._demand is not None:
                # patches/myhome/0003: the central's demand frames are authoritative; a zone has several
                # actuators (valve, fan coil) whose individual states would make hvac_action flap.
                self._apply_demand()
            elif message.is_active():
                if self._heating and self._cooling:
                    if message.is_heating():
                        self._attr_hvac_action = HVACAction.HEATING
                    elif message.is_cooling():
                        self._attr_hvac_action = HVACAction.COOLING
                    else:  # patches/myhome/0003: dimension 20 has no season flag; use the plant season
                        self._attr_hvac_action = self._active_action()
                elif self._heating:
                    self._attr_hvac_action = HVACAction.HEATING
                elif self._cooling:
                    self._attr_hvac_action = HVACAction.COOLING
            elif self._attr_hvac_mode == HVACMode.OFF:
                self._attr_hvac_action = HVACAction.OFF
            else:
                self._attr_hvac_action = HVACAction.IDLE

        if message.message_type in (MESSAGE_TYPE_MODE, MESSAGE_TYPE_MODE_TARGET) and self._demand is not None:
            self._apply_demand()

        target = self.target_temperature
        if self._attr_hvac_mode in (HVACMode.COOL, HVACMode.HEAT) and target is not None and 10 <= target <= 32:
            self._resume_setpoint = target

        self.async_schedule_update_ha_state()

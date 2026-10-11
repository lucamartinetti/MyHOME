"""Code to handle a MyHome Gateway."""
import asyncio
import re
from typing import Dict, List

from homeassistant.const import (
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_PASSWORD,
    CONF_NAME,
    CONF_MAC,
    CONF_FRIENDLY_NAME,
)
from homeassistant.components.light import DOMAIN as LIGHT
from homeassistant.components.switch import (
    SwitchDeviceClass,
    DOMAIN as SWITCH,
)
from homeassistant.components.button import DOMAIN as BUTTON
from homeassistant.components.cover import DOMAIN as COVER
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    DOMAIN as BINARY_SENSOR,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    DOMAIN as SENSOR,
)
from homeassistant.components.climate import DOMAIN as CLIMATE

from OWNd.connection import OWNSession, OWNEventSession, OWNCommandSession, OWNGateway
from OWNd.message import (
    OWNMessage,
    OWNLightingEvent,
    OWNLightingCommand,
    OWNEnergyEvent,
    OWNAutomationEvent,
    OWNDryContactEvent,
    OWNAuxEvent,
    OWNHeatingEvent,
    OWNHeatingCommand,
    OWNCENPlusEvent,
    OWNCENEvent,
    OWNGatewayEvent,
    OWNGatewayCommand,
    OWNCommand,
)

from .const import (
    CONF_PLATFORMS,
    CONF_FIRMWARE,
    CONF_SSDP_LOCATION,
    CONF_SSDP_ST,
    CONF_DEVICE_TYPE,
    CONF_MANUFACTURER,
    CONF_MANUFACTURER_URL,
    CONF_UDN,
    CONF_SHORT_PRESS,
    CONF_SHORT_RELEASE,
    CONF_LONG_PRESS,
    CONF_LONG_RELEASE,
    DOMAIN,
    LOGGER,
)
from .myhome_device import MyHOMEEntity
from .button import (
    DisableCommandButtonEntity,
    EnableCommandButtonEntity,
)


class MyHOMEGatewayHandler:
    """Manages a single MyHOME Gateway."""

    def __init__(self, hass, config_entry, generate_events=False):
        build_info = {
            "address": config_entry.data[CONF_HOST],
            "port": config_entry.data[CONF_PORT],
            "password": config_entry.data[CONF_PASSWORD],
            "ssdp_location": config_entry.data[CONF_SSDP_LOCATION],
            "ssdp_st": config_entry.data[CONF_SSDP_ST],
            "deviceType": config_entry.data[CONF_DEVICE_TYPE],
            "friendlyName": config_entry.data[CONF_FRIENDLY_NAME],
            "manufacturer": config_entry.data[CONF_MANUFACTURER],
            "manufacturerURL": config_entry.data[CONF_MANUFACTURER_URL],
            "modelName": config_entry.data[CONF_NAME],
            "modelNumber": config_entry.data[CONF_FIRMWARE],
            "serialNumber": config_entry.data[CONF_MAC],
            "UDN": config_entry.data[CONF_UDN],
        }
        self.hass = hass
        self.config_entry = config_entry
        self.generate_events = generate_events
        self.gateway = OWNGateway(build_info)
        self._terminate_listener = False
        self._terminate_sender = False
        self.is_connected = False
        self.listening_worker: asyncio.tasks.Task = None
        self.sending_workers: List[asyncio.tasks.Task] = []
        self.send_buffer = asyncio.Queue()

    @property
    def mac(self) -> str:
        return self.gateway.serial

    @property
    def unique_id(self) -> str:
        return self.mac

    @property
    def log_id(self) -> str:
        return self.gateway.log_id

    @property
    def manufacturer(self) -> str:
        # SSDP discovery can store the manufacturer as a list; the device registry wants a string (2026.12+).
        manufacturer = self.gateway.manufacturer
        if isinstance(manufacturer, (list, tuple)):
            return ", ".join(str(m) for m in manufacturer)
        return manufacturer

    @property
    def name(self) -> str:
        return f"{self.gateway.model_name} Gateway"

    @property
    def model(self) -> str:
        return self.gateway.model_name

    @property
    def firmware(self) -> str:
        # Same as manufacturer: may arrive as a list; the device registry wants a string (2026.12+).
        firmware = self.gateway.firmware
        if isinstance(firmware, (list, tuple)):
            return ", ".join(str(f) for f in firmware)
        return firmware

    async def test(self) -> Dict:
        return await OWNSession(gateway=self.gateway, logger=LOGGER).test_connection()

    # Local patch (homeassistant-setup patches/myhome/0003): thermoregulation central frames.
    #   `*#4*0#N*20*V##`        state of central output ("pump") N, V 0 = off
    #   `*4*4001#Z*0#N##`       zone Z requests output N (4002 = releases it)
    #   `*4*4001*Z##`           zone Z demand on (4002 = off)
    # Stock OWNd parses these as zone N / zone Z frames (output 6 lands on zone 6's climate entity), so they are
    # handled here and NOT dispatched to entities. They are fired as HA events (`myhome_thermo_output`,
    # `myhome_thermo_request`) and the zone demand is passed to that zone's climate entity (hvac_action).
    _RE_THERMO_OUTPUT = re.compile(r"^\*#4\*0#(\d+)\*20\*(\d+)##$")
    _RE_THERMO_REQUEST = re.compile(r"^\*4\*(4001|4002)#(\d+)\*0#(\d+)##$")
    _RE_THERMO_DEMAND = re.compile(r"^\*4\*(4001|4002)\*(\d+)##$")

    def _handle_thermo_central(self, frame: str) -> bool:
        if m := self._RE_THERMO_OUTPUT.match(frame):
            self.hass.bus.async_fire(
                "myhome_thermo_output",
                {"gateway": str(self.gateway.host), "output": int(m[1]), "on": m[2] != "0", "value": int(m[2])},
            )
            return True
        if m := self._RE_THERMO_REQUEST.match(frame):
            zone, output, on = int(m[2]), int(m[3]), m[1] == "4001"
            self.hass.bus.async_fire(
                "myhome_thermo_request",
                {"gateway": str(self.gateway.host), "zone": zone, "output": output, "on": on},
            )
            self._thermo_zone_demand(zone, on)
            return True
        if m := self._RE_THERMO_DEMAND.match(frame):
            self._thermo_zone_demand(int(m[2]), m[1] == "4001")
            return True
        return False

    def _thermo_zone_demand(self, zone: int, on: bool):
        climates = self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS].get(CLIMATE, {})
        device = climates.get(f"4-{zone}")
        if device is None:
            return
        for _entity in device[CONF_ENTITIES].values():
            if hasattr(_entity, "handle_demand"):
                _entity.handle_demand(on)

    async def listening_loop(self):
        """Supervises the event session: reconnects with backoff and never lets a single message end the listener."""
        self._terminate_listener = False

        LOGGER.debug("%s Creating listening worker.", self.log_id)

        _backoff = 1
        try:
            while not self._terminate_listener:
                _event_session = OWNEventSession(gateway=self.gateway, logger=LOGGER)
                try:
                    await _event_session.connect()
                    self.is_connected = True
                    _backoff = 1
                    await self._listen(_event_session)
                except asyncio.CancelledError:
                    raise
                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception("%s Event session failed, reconnecting in %ss.", self.log_id, _backoff)
                finally:
                    self.is_connected = False
                    try:
                        await _event_session.close()
                    except Exception:  # pylint: disable=broad-except
                        pass
                if not self._terminate_listener:
                    await asyncio.sleep(_backoff)
                    _backoff = min(_backoff * 2, 60)
        except asyncio.CancelledError:
            LOGGER.debug("%s Listening worker cancelled.", self.log_id)
            raise
        except BaseException:
            LOGGER.error("%s Listening worker stopped unexpectedly, bus events will not be received.", self.log_id)
            raise

        LOGGER.debug("%s Destroying listening worker.", self.log_id)

    async def _listen(self, event_session: OWNEventSession):
        # OWNd's get_next() swallows connection errors and returns None; a run of them means the session is dead.
        _empty_reads = 0
        while not self._terminate_listener:
            message = await event_session.get_next()
            if message is None:
                _empty_reads += 1
                if _empty_reads >= 10:
                    raise ConnectionError("event session keeps returning no data")
                await asyncio.sleep(0.5)
                continue
            _empty_reads = 0
            try:
                await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                # One bad message (unknown address, entity not added yet, entry unloading) must not kill the listener.
                LOGGER.exception("%s Error handling message `%s`, skipping it.", self.log_id, message)

    async def _handle_message(self, message):
        LOGGER.debug("%s Message received: `%s`", self.log_id, message)

        if self.generate_events:
            if isinstance(message, OWNMessage):
                _event_content = {"gateway": str(self.gateway.host)}
                _event_content.update(message.event_content)
                self.hass.bus.async_fire("myhome_message_event", _event_content)
            else:
                self.hass.bus.async_fire("myhome_message_event", {"gateway": str(self.gateway.host), "message": str(message)})

        if not isinstance(message, OWNMessage):
            LOGGER.warning(
                "%s Data received is not a message: `%s`",
                self.log_id,
                message,
            )
        elif self._handle_thermo_central(str(message)):
            pass
        elif isinstance(message, OWNEnergyEvent):
            if SENSOR in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS] and message.entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR]:
                for _entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR][message.entity][CONF_ENTITIES]:
                    if isinstance(
                        self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR][message.entity][CONF_ENTITIES][_entity],
                        MyHOMEEntity,
                    ):
                        self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][SENSOR][message.entity][CONF_ENTITIES][_entity].handle_event(message)
            else:
                return
        elif (
            isinstance(message, OWNLightingEvent)
            or isinstance(message, OWNAutomationEvent)
            or isinstance(message, OWNDryContactEvent)
            or isinstance(message, OWNAuxEvent)
            or isinstance(message, OWNHeatingEvent)
        ):
            if not message.is_translation:
                is_event = False
                if isinstance(message, OWNLightingEvent):
                    if message.is_general:
                        is_event = True
                        event = "on" if message.is_on else "off"
                        self.hass.bus.async_fire(
                            "myhome_general_light_event",
                            {"message": str(message), "event": event},
                        )
                        await asyncio.sleep(0.1)
                        await self.send_status_request(OWNLightingCommand.status("0"))
                    elif message.is_area:
                        is_event = True
                        event = "on" if message.is_on else "off"
                        self.hass.bus.async_fire(
                            "myhome_area_light_event",
                            {
                                "message": str(message),
                                "area": message.area,
                                "event": event,
                            },
                        )
                        await asyncio.sleep(0.1)
                        await self.send_status_request(OWNLightingCommand.status(message.area))
                    elif message.is_group:
                        is_event = True
                        event = "on" if message.is_on else "off"
                        self.hass.bus.async_fire(
                            "myhome_group_light_event",
                            {
                                "message": str(message),
                                "group": message.group,
                                "event": event,
                            },
                        )
                elif isinstance(message, OWNAutomationEvent):
                    if message.is_general:
                        is_event = True
                        if message.is_opening and not message.is_closing:
                            event = "open"
                        elif message.is_closing and not message.is_opening:
                            event = "close"
                        else:
                            event = "stop"
                        self.hass.bus.async_fire(
                            "myhome_general_automation_event",
                            {"message": str(message), "event": event},
                        )
                    elif message.is_area:
                        is_event = True
                        if message.is_opening and not message.is_closing:
                            event = "open"
                        elif message.is_closing and not message.is_opening:
                            event = "close"
                        else:
                            event = "stop"
                        self.hass.bus.async_fire(
                            "myhome_area_automation_event",
                            {
                                "message": str(message),
                                "area": message.area,
                                "event": event,
                            },
                        )
                    elif message.is_group:
                        is_event = True
                        if message.is_opening and not message.is_closing:
                            event = "open"
                        elif message.is_closing and not message.is_opening:
                            event = "close"
                        else:
                            event = "stop"
                        self.hass.bus.async_fire(
                            "myhome_group_automation_event",
                            {
                                "message": str(message),
                                "group": message.group,
                                "event": event,
                            },
                        )
                if not is_event:
                    if isinstance(message, OWNLightingEvent) and message.brightness_preset:
                        # CONF_ENTITIES[LIGHT] only exists once the entity is added to hass (not yet at startup, never for an
                        # unconfigured address): indexing it directly raised KeyError and killed the listening loop.
                        _light = (
                            self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS].get(LIGHT, {}).get(message.entity, {}).get(CONF_ENTITIES, {}).get(LIGHT)
                        )
                        if isinstance(_light, MyHOMEEntity):
                            await _light.async_update()
                        else:
                            LOGGER.debug("%s No light entity for `%s`, ignoring `%s`", self.log_id, message.entity, message)
                    else:
                        for _platform in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS]:
                            if _platform != BUTTON and message.entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform]:
                                for _entity in self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES]:
                                    if (
                                        isinstance(
                                            self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity],
                                            MyHOMEEntity,
                                        )
                                        and not isinstance(
                                            self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity],
                                            DisableCommandButtonEntity,
                                        )
                                        and not isinstance(
                                            self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity],
                                            EnableCommandButtonEntity,
                                        )
                                    ):
                                        self.hass.data[DOMAIN][self.mac][CONF_PLATFORMS][_platform][message.entity][CONF_ENTITIES][_entity].handle_event(message)

            else:
                LOGGER.debug(
                    "%s Ignoring translation message `%s`",
                    self.log_id,
                    message,
                )
        elif isinstance(message, OWNHeatingCommand) and message.dimension is not None and message.dimension == 14:
            where = message.where[1:] if message.where.startswith("#") else message.where
            LOGGER.debug(
                "%s Received heating command, sending query to zone %s",
                self.log_id,
                where,
            )
            await self.send_status_request(OWNHeatingCommand.status(where))
        elif isinstance(message, OWNCENPlusEvent):
            event = None
            if message.is_short_pressed:
                event = CONF_SHORT_PRESS
            elif message.is_held or message.is_still_held:
                event = CONF_LONG_PRESS
            elif message.is_released:
                event = CONF_LONG_RELEASE
            else:
                event = None
            self.hass.bus.async_fire(
                "myhome_cenplus_event",
                {
                    "object": int(message.object),
                    "pushbutton": int(message.push_button),
                    "event": event,
                },
            )
            LOGGER.info(
                "%s %s",
                self.log_id,
                message.human_readable_log,
            )
        elif isinstance(message, OWNCENEvent):
            event = None
            if message.is_pressed:
                event = CONF_SHORT_PRESS
            elif message.is_released_after_short_press:
                event = CONF_SHORT_RELEASE
            elif message.is_held:
                event = CONF_LONG_PRESS
            elif message.is_released_after_long_press:
                event = CONF_LONG_RELEASE
            else:
                event = None
            self.hass.bus.async_fire(
                "myhome_cen_event",
                {
                    "object": int(message.object),
                    "pushbutton": int(message.push_button),
                    "event": event,
                },
            )
            LOGGER.info(
                "%s %s",
                self.log_id,
                message.human_readable_log,
            )
        elif isinstance(message, OWNGatewayEvent) or isinstance(message, OWNGatewayCommand):
            LOGGER.info(
                "%s %s",
                self.log_id,
                message.human_readable_log,
            )
        else:
            LOGGER.info(
                "%s Unsupported message type: `%s`",
                self.log_id,
                message,
            )

    async def sending_loop(self, worker_id: int):
        self._terminate_sender = False

        LOGGER.debug(
            "%s Creating sending worker %s",
            self.log_id,
            worker_id,
        )

        _command_session = OWNCommandSession(gateway=self.gateway, logger=LOGGER)
        await _command_session.connect()

        try:
            while not self._terminate_sender:
                task = await self.send_buffer.get()
                LOGGER.debug(
                    "%s Message `%s` was successfully unqueued by worker %s.",
                    self.log_id,
                    task["message"],
                    worker_id,
                )
                await _command_session.send(message=task["message"], is_status_request=task["is_status_request"])
                self.send_buffer.task_done()
        finally:
            await _command_session.close()

        LOGGER.debug(
            "%s Destroying sending worker %s",
            self.log_id,
            worker_id,
        )

    async def close_listener(self) -> bool:
        LOGGER.info("%s Closing event listener", self.log_id)
        self._terminate_sender = True
        self._terminate_listener = True

        # The workers block in get_next()/queue.get(): the flags alone leave them running against the next entry's
        # hass.data after a reload ("Task was destroyed but it is pending"), so cancel and reap them.
        _workers = [w for w in [self.listening_worker, *self.sending_workers] if w is not None and not w.done()]
        for _worker in _workers:
            _worker.cancel()
        await asyncio.gather(*_workers, return_exceptions=True)
        self.listening_worker = None
        self.sending_workers.clear()

        return True

    async def send(self, message: OWNCommand):
        await self.send_buffer.put({"message": message, "is_status_request": False})
        LOGGER.debug(
            "%s Message `%s` was successfully queued.",
            self.log_id,
            message,
        )

    async def send_status_request(self, message: OWNCommand):
        await self.send_buffer.put({"message": message, "is_status_request": True})
        LOGGER.debug(
            "%s Message `%s` was successfully queued.",
            self.log_id,
            message,
        )

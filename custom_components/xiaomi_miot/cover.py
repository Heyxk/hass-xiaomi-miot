"""Support for Curtain and Airer."""
import logging
from datetime import timedelta

from homeassistant.components.cover import (
    DOMAIN as ENTITY_DOMAIN,
    CoverEntity as BaseEntity,
    CoverEntityFeature,  # v2022.5
)

from . import (
    DOMAIN,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    HassEntry,
    XEntity,
    async_setup_config_entry,
    bind_services_to_entries,
)
from .core.miot_spec import MiotProperty
from .core.converters import MiotPropConv, MiotTargetPositionConv

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'
SCAN_INTERVAL = timedelta(seconds=60)

SERVICE_TO_METHOD = {}


async def async_setup_entry(hass, config_entry, async_add_entities):
    HassEntry.init(hass, config_entry).new_adder(ENTITY_DOMAIN, async_add_entities)
    await async_setup_config_entry(hass, config_entry, async_setup_platform, async_add_entities, ENTITY_DOMAIN)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})
    hass.data[DOMAIN]['add_entities'][ENTITY_DOMAIN] = async_add_entities
    config['hass'] = hass
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class CoverEntity(XEntity, BaseEntity):
    _attr_is_closed = None
    _attr_target_cover_position = None
    _attr_supported_features = CoverEntityFeature(0)
    _conv_status = None
    _conv_motor: MiotPropConv = None
    _conv_current_position = None
    _conv_target_position = None
    _current_range = None
    _target_range = (0, 100)
    _motor_reverse = None
    _position_reverse = None
    _open_texts = ['Open', 'Up']
    _close_texts = ['Close', 'Down']
    _closed_position = 0
    _deviated_position = 0
    _target2current_position = None

    def on_init(self):
        self._motor_reverse = self.custom_config_bool('motor_reverse', False)
        self._position_reverse = self.custom_config_bool('position_reverse', self._motor_reverse)
        self._open_texts = self.custom_config_list('open_texts', self._open_texts)
        self._close_texts = self.custom_config_list('close_texts', self._close_texts)
        if self._motor_reverse:
            self._open_texts, self._close_texts = self._close_texts, self._open_texts

        for conv in self.device.converters:
            prop = getattr(conv, 'prop', None)
            if not isinstance(prop, MiotProperty):
                continue
            elif prop.in_list(['status']):
                self._conv_status = conv
            elif prop.in_list(['motor_control']):
                self._conv_motor = conv
                self._attr_supported_features |= CoverEntityFeature.OPEN
                self._attr_supported_features |= CoverEntityFeature.CLOSE
                if prop.list_first('Stop', 'Pause') != None:
                    self._attr_supported_features |= CoverEntityFeature.STOP
            elif prop.value_range and prop.in_list(['current_position']):
                self._conv_current_position = conv
                self._current_range = (prop.range_min(), prop.range_max())
            elif prop.value_range and isinstance(conv, MiotTargetPositionConv):
                self._conv_target_position = conv
                self._target_range = conv.ranged
                self._attr_supported_features |= CoverEntityFeature.SET_POSITION
            elif prop.value_range and prop.in_list(['target_position']):
                self._conv_target_position = conv
                self._target_range = (prop.range_min(), prop.range_max())
                self._attr_supported_features |= CoverEntityFeature.SET_POSITION

        self._deviated_position = self.custom_config_integer('deviated_position', 2)
        if self._current_range:
            pos = self._current_range[0] + self._deviated_position
            self._closed_position = self.custom_config_integer('closed_position', pos)
        self._target2current_position = self.custom_config_bool('target2current_position', not self._conv_current_position)

        if self._motor_reverse or self._position_reverse:
            self._attr_extra_state_attributes.update({
                'motor_reverse': self._motor_reverse,
                'position_reverse': self._position_reverse,
            })
        if self._closed_position:
            self._attr_extra_state_attributes.update({
                'closed_position': self._closed_position,
                'deviated_position': self._deviated_position,
            })

    def set_state(self, data: dict):
        prop_status = getattr(self._conv_status, 'prop', None) if self._conv_status else None
        if prop_status:
            val = self._conv_status.value_from_dict(data)
            self._attr_is_opening = val in prop_status.list_search('Opening', 'Rising')
            self._attr_is_closing = val in prop_status.list_search('Closing', 'Falling')
            self._attr_is_closed  = val in prop_status.list_search('Closed')
        if self._conv_current_position:
            val = self._conv_current_position.value_from_dict(data)
            if val is not None:
                val = int(val)
                if self._position_reverse:
                    val = self._current_range[1] - val
                self._attr_current_cover_position = val
        if self._conv_target_position:
            val = self._conv_target_position.value_from_dict(data)
            if val is not None:
                val = int(val)
                if self._position_reverse:
                    val = self._target_range[1] - val
                self._attr_target_cover_position = val
        if self._target2current_position:
            self._attr_current_cover_position = self._attr_target_cover_position
            self._attr_extra_state_attributes.update({
                'target2current_position': True,
            })
        if (val := self._attr_current_cover_position) != None:
            self._attr_is_closed = val <= self._closed_position

    async def async_open_cover(self, **kwargs):
        if self._conv_motor:
            val = self._conv_motor.prop.list_first(self._open_texts)
            if val is not None:
                await self.device.async_write({self._conv_motor.full_name: val})
                return
        await self.async_set_cover_position(0 if self._position_reverse else 100)

    async def async_close_cover(self, **kwargs):
        if self._conv_motor:
            val = self._conv_motor.prop.list_first(self._close_texts)
            if val is not None:
                await self.device.async_write({self._conv_motor.full_name: val})
                return
        await self.async_set_cover_position(100 if self._position_reverse else 0)

    async def async_stop_cover(self, **kwargs):
        if not self._conv_motor:
            return
        val = self._conv_motor.prop.list_first('Stop', 'Pause')
        if val is not None:
            await self.device.async_write({self._conv_motor.full_name: val})

    async def async_set_cover_position(self, position, **kwargs):
        if not self._conv_target_position:
            return
        if self._position_reverse:
            position = self._target_range[1] - position
        await self.device.async_write({self._conv_target_position.full_name: position})

XEntity.CLS[ENTITY_DOMAIN] = CoverEntity

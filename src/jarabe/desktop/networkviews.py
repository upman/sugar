# Copyright (C) 2006-2007 Red Hat, Inc.
# Copyright (C) 2009 Tomeu Vizoso, Simon Schampijer
# Copyright (C) 2009-2010 One Laptop per Child
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

from gettext import gettext as _
import logging
import hashlib

import dbus
import glib

from sugar.graphics.icon import Icon
from sugar.graphics.xocolor import XoColor
from sugar.graphics import xocolor
from sugar.graphics import style
from sugar.graphics.icon import get_icon_state
from sugar.graphics import palette
from sugar.graphics.menuitem import MenuItem
from sugar.util import unique_id
from sugar import profile

from jarabe.view.pulsingicon import CanvasPulsingIcon
from jarabe.desktop import keydialog
from jarabe.model import network
from jarabe.model.network import Settings
from jarabe.model.network import IP4Config
from jarabe.model.network import WirelessSecurity
from jarabe.model.adhoc import get_adhoc_manager_instance


_NM_SERVICE = 'org.freedesktop.NetworkManager'
_NM_IFACE = 'org.freedesktop.NetworkManager'
_NM_PATH = '/org/freedesktop/NetworkManager'
_NM_DEVICE_IFACE = 'org.freedesktop.NetworkManager.Device'
_NM_WIRELESS_IFACE = 'org.freedesktop.NetworkManager.Device.Wireless'
_NM_OLPC_MESH_IFACE = 'org.freedesktop.NetworkManager.Device.OlpcMesh'
_NM_ACCESSPOINT_IFACE = 'org.freedesktop.NetworkManager.AccessPoint'
_NM_ACTIVE_CONN_IFACE = 'org.freedesktop.NetworkManager.Connection.Active'

_AP_ICON_NAME = 'network-wireless'
_OLPC_MESH_ICON_NAME = 'network-mesh'


class WirelessNetworkView(CanvasPulsingIcon):
    def __init__(self, initial_ap):
        CanvasPulsingIcon.__init__(self, size=style.STANDARD_ICON_SIZE,
                                   cache=True)
        self._bus = dbus.SystemBus()
        self._access_points = {initial_ap.model.object_path: initial_ap}
        self._active_ap = None
        self._device = initial_ap.device
        self._palette_icon = None
        self._disconnect_item = None
        self._connect_item = None
        self._greyed_out = False
        self._name = initial_ap.name
        self._mode = initial_ap.mode
        self._strength = initial_ap.strength
        self._flags = initial_ap.flags
        self._wpa_flags = initial_ap.wpa_flags
        self._rsn_flags = initial_ap.rsn_flags
        self._device_caps = 0
        self._device_state = None
        self._color = None

        if self._mode == network.NM_802_11_MODE_ADHOC and \
                network.is_sugar_adhoc_network(self._name):
            self._color = profile.get_color()
        else:
            sha_hash = hashlib.sha1()
            data = self._name + hex(self._flags)
            sha_hash.update(data)
            digest = hash(sha_hash.digest())
            index = digest % len(xocolor.colors)

            self._color = xocolor.XoColor('%s,%s' %
                                          (xocolor.colors[index][0],
                                           xocolor.colors[index][1]))

        self.connect('button-release-event', self.__button_release_event_cb)

        pulse_color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                         style.COLOR_TRANSPARENT.get_svg()))
        self.props.pulse_color = pulse_color

        self._palette = self._create_palette()
        self.set_palette(self._palette)
        self._palette_icon.props.xo_color = self._color
        self._update_badge()

        interface_props = dbus.Interface(self._device, dbus.PROPERTIES_IFACE)
        interface_props.Get(_NM_DEVICE_IFACE, 'State',
                            reply_handler=self.__get_device_state_reply_cb,
                            error_handler=self.__get_device_state_error_cb)
        interface_props.Get(_NM_WIRELESS_IFACE, 'WirelessCapabilities',
                            reply_handler=self.__get_device_caps_reply_cb,
                            error_handler=self.__get_device_caps_error_cb)
        interface_props.Get(_NM_WIRELESS_IFACE, 'ActiveAccessPoint',
                            reply_handler=self.__get_active_ap_reply_cb,
                            error_handler=self.__get_active_ap_error_cb)

        self._bus.add_signal_receiver(self.__device_state_changed_cb,
                                      signal_name='StateChanged',
                                      path=self._device.object_path,
                                      dbus_interface=_NM_DEVICE_IFACE)
        self._bus.add_signal_receiver(self.__wireless_properties_changed_cb,
                                      signal_name='PropertiesChanged',
                                      path=self._device.object_path,
                                      dbus_interface=_NM_WIRELESS_IFACE)

    def _create_palette(self):
        icon_name = get_icon_state(_AP_ICON_NAME, self._strength)
        self._palette_icon = Icon(icon_name=icon_name,
                                  icon_size=style.STANDARD_ICON_SIZE,
                                  badge_name=self.props.badge_name)

        p = palette.Palette(primary_text=glib.markup_escape_text(self._name),
                            icon=self._palette_icon)

        self._connect_item = MenuItem(_('Connect'), 'dialog-ok')
        self._connect_item.connect('activate', self.__connect_activate_cb)
        p.menu.append(self._connect_item)

        self._disconnect_item = MenuItem(_('Disconnect'), 'media-eject')
        self._disconnect_item.connect('activate',
                                        self._disconnect_activate_cb)
        p.menu.append(self._disconnect_item)

        return p

    def __device_state_changed_cb(self, new_state, old_state, reason):
        self._device_state = new_state
        self._update_state()
        self._update_icon()
        self._update_badge()

    def __update_active_ap(self, ap_path):
        if ap_path in self._access_points:
            # save reference to active AP, so that we always display the
            # strength of that one
            self._active_ap = self._access_points[ap_path]
            self.update_strength()
        elif self._active_ap is not None:
            # revert to showing state of strongest AP again
            self._active_ap = None
            self.update_strength()

    def __wireless_properties_changed_cb(self, properties):
        if 'ActiveAccessPoint' in properties:
            self.__update_active_ap(properties['ActiveAccessPoint'])

    def __get_active_ap_reply_cb(self, ap_path):
        self.__update_active_ap(ap_path)

    def __get_active_ap_error_cb(self, err):
        logging.error('Error getting the active access point: %s', err)

    def __get_device_caps_reply_cb(self, caps):
        self._device_caps = caps

    def __get_device_caps_error_cb(self, err):
        logging.error('Error getting the wireless device properties: %s', err)

    def __get_device_state_reply_cb(self, state):
        self._device_state = state
        self._update_state()
        self._update_color()
        self._update_badge()

    def __get_device_state_error_cb(self, err):
        logging.error('Error getting the device state: %s', err)

    def _update_icon(self):
        if self._mode == network.NM_802_11_MODE_ADHOC and \
                network.is_sugar_adhoc_network(self._name):
            channel = max([1] + [ap.channel for ap in
                                 self._access_points.values()])
            if self._device_state == network.DEVICE_STATE_ACTIVATED and \
                    self._active_ap is not None:
                icon_name = 'network-adhoc-%s-connected' % channel
            else:
                icon_name = 'network-adhoc-%s' % channel
            self.props.icon_name = icon_name
            icon = self._palette.props.icon
            icon.props.icon_name = icon_name
        else:
            if self._device_state == network.DEVICE_STATE_ACTIVATED and \
                    self._active_ap is not None:
                icon_name = '%s-connected' % _AP_ICON_NAME
            else:
                icon_name = _AP_ICON_NAME

            icon_name = get_icon_state(icon_name, self._strength)
            if icon_name:
                self.props.icon_name = icon_name
                icon = self._palette.props.icon
                icon.props.icon_name = icon_name

    def _update_badge(self):
        if self._mode != network.NM_802_11_MODE_ADHOC:
            if network.find_connection_by_ssid(self._name) is not None:
                self.props.badge_name = 'emblem-favorite'
                self._palette_icon.props.badge_name = 'emblem-favorite'
            elif self._flags == network.NM_802_11_AP_FLAGS_PRIVACY:
                self.props.badge_name = 'emblem-locked'
                self._palette_icon.props.badge_name = 'emblem-locked'
            else:
                self.props.badge_name = None
                self._palette_icon.props.badge_name = None
        else:
            self.props.badge_name = None
            self._palette_icon.props.badge_name = None

    def _update_state(self):
        if self._active_ap is not None:
            state = self._device_state
        else:
            state = network.DEVICE_STATE_UNKNOWN

        if state == network.DEVICE_STATE_PREPARE or \
           state == network.DEVICE_STATE_CONFIG or \
           state == network.DEVICE_STATE_NEED_AUTH or \
           state == network.DEVICE_STATE_IP_CONFIG:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connecting...')
            self.props.pulsing = True
        elif state == network.DEVICE_STATE_ACTIVATED:
            connection = network.find_connection_by_ssid(self._name)
            if connection is not None:
                if self._mode == network.NM_802_11_MODE_INFRA:
                    connection.set_connected()
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connected')
            self.props.pulsing = False
        else:
            if self._disconnect_item:
                self._disconnect_item.hide()
            self._connect_item.show()
            self._palette.props.secondary_text = None
            self.props.pulsing = False

    def _update_color(self):
        if self._greyed_out:
            self.props.pulsing = False
            self.props.base_color = XoColor('#D5D5D5,#D5D5D5')
        else:
            self.props.base_color = self._color

    def _disconnect_activate_cb(self, item):
        pass

    def _add_ciphers_from_flags(self, flags, pairwise):
        ciphers = []
        if pairwise:
            if flags & network.NM_802_11_AP_SEC_PAIR_TKIP:
                ciphers.append('tkip')
            if flags & network.NM_802_11_AP_SEC_PAIR_CCMP:
                ciphers.append('ccmp')
        else:
            if flags & network.NM_802_11_AP_SEC_GROUP_WEP40:
                ciphers.append('wep40')
            if flags & network.NM_802_11_AP_SEC_GROUP_WEP104:
                ciphers.append('wep104')
            if flags & network.NM_802_11_AP_SEC_GROUP_TKIP:
                ciphers.append('tkip')
            if flags & network.NM_802_11_AP_SEC_GROUP_CCMP:
                ciphers.append('ccmp')
        return ciphers

    def _get_security(self):
        if not (self._flags & network.NM_802_11_AP_FLAGS_PRIVACY) and \
                (self._wpa_flags == network.NM_802_11_AP_SEC_NONE) and \
                (self._rsn_flags == network.NM_802_11_AP_SEC_NONE):
            # No security
            return None

        if (self._flags & network.NM_802_11_AP_FLAGS_PRIVACY) and \
                (self._wpa_flags == network.NM_802_11_AP_SEC_NONE) and \
                (self._rsn_flags == network.NM_802_11_AP_SEC_NONE):
            # Static WEP, Dynamic WEP, or LEAP
            wireless_security = WirelessSecurity()
            wireless_security.key_mgmt = 'none'
            return wireless_security

        if (self._mode != network.NM_802_11_MODE_INFRA):
            # Stuff after this point requires infrastructure
            logging.error('The infrastructure mode is not supoorted'
                          ' by your wireless device.')
            return None

        if (self._rsn_flags & network.NM_802_11_AP_SEC_KEY_MGMT_PSK) and \
                (self._device_caps & network.NM_802_11_DEVICE_CAP_RSN):
            # WPA2 PSK first
            pairwise = self._add_ciphers_from_flags(self._rsn_flags, True)
            group = self._add_ciphers_from_flags(self._rsn_flags, False)
            wireless_security = WirelessSecurity()
            wireless_security.key_mgmt = 'wpa-psk'
            wireless_security.proto = 'rsn'
            wireless_security.pairwise = pairwise
            wireless_security.group = group
            return wireless_security

        if (self._wpa_flags & network.NM_802_11_AP_SEC_KEY_MGMT_PSK) and \
                (self._device_caps & network.NM_802_11_DEVICE_CAP_WPA):
            # WPA PSK
            pairwise = self._add_ciphers_from_flags(self._wpa_flags, True)
            group = self._add_ciphers_from_flags(self._wpa_flags, False)
            wireless_security = WirelessSecurity()
            wireless_security.key_mgmt = 'wpa-psk'
            wireless_security.proto = 'wpa'
            wireless_security.pairwise = pairwise
            wireless_security.group = group
            return wireless_security

    def __connect_activate_cb(self, icon):
        self._connect()

    def __button_release_event_cb(self, icon, event):
        self._connect()

    def _connect(self):
        connection = network.find_connection_by_ssid(self._name)
        if connection is None:
            settings = Settings()
            settings.connection.id = 'Auto ' + self._name
            uuid = settings.connection.uuid = unique_id()
            settings.connection.type = '802-11-wireless'
            settings.wireless.ssid = self._name

            if self._mode == network.NM_802_11_MODE_INFRA:
                settings.wireless.mode = 'infrastructure'
            elif self._mode == network.NM_802_11_MODE_ADHOC:
                settings.wireless.mode = 'adhoc'
                settings.wireless.band = 'bg'
                settings.ip4_config = IP4Config()
                settings.ip4_config.method = 'link-local'

            wireless_security = self._get_security()
            settings.wireless_security = wireless_security

            if wireless_security is not None:
                settings.wireless.security = '802-11-wireless-security'

            connection = network.add_connection(uuid, settings)

        obj = self._bus.get_object(_NM_SERVICE, _NM_PATH)
        netmgr = dbus.Interface(obj, _NM_IFACE)

        netmgr.ActivateConnection(network.SETTINGS_SERVICE, connection.path,
                                  self._device.object_path,
                                  '/',
                                  reply_handler=self.__activate_reply_cb,
                                  error_handler=self.__activate_error_cb)

    def __activate_reply_cb(self, connection):
        logging.debug('Connection activated: %s', connection)

    def __activate_error_cb(self, err):
        logging.error('Failed to activate connection: %s', err)

    def set_filter(self, query):
        self._greyed_out = self._name.lower().find(query) == -1
        self._update_icon()
        self._update_color()

    def create_keydialog(self, settings, response):
        keydialog.create(self._name, self._flags, self._wpa_flags,
                         self._rsn_flags, self._device_caps, settings,
                         response)

    def update_strength(self):
        if self._active_ap is not None:
            # display strength of AP that we are connected to
            new_strength = self._active_ap.strength
        else:
            # display the strength of the strongest AP that makes up this
            # network, also considering that there may be no APs
            new_strength = max([0] + [ap.strength for ap in
                                      self._access_points.values()])

        if new_strength != self._strength:
            self._strength = new_strength
            self._update_icon()

    def add_ap(self, ap):
        self._access_points[ap.model.object_path] = ap
        self.update_strength()

    def remove_ap(self, ap):
        path = ap.model.object_path
        if path not in self._access_points:
            return
        del self._access_points[path]
        if self._active_ap == ap:
            self._active_ap = None
        self.update_strength()

    def num_aps(self):
        return len(self._access_points)

    def find_ap(self, ap_path):
        if ap_path not in self._access_points:
            return None
        return self._access_points[ap_path]

    def is_olpc_mesh(self):
        return self._mode == network.NM_802_11_MODE_ADHOC \
            and self.name == 'olpc-mesh'

    def remove_all_aps(self):
        for ap in self._access_points.values():
            ap.disconnect()
        self._access_points = {}
        self._active_ap = None
        self.update_strength()

    def disconnect(self):
        self._bus.remove_signal_receiver(self.__device_state_changed_cb,
                                         signal_name='StateChanged',
                                         path=self._device.object_path,
                                         dbus_interface=_NM_DEVICE_IFACE)
        self._bus.remove_signal_receiver(self.__wireless_properties_changed_cb,
                                         signal_name='PropertiesChanged',
                                         path=self._device.object_path,
                                         dbus_interface=_NM_WIRELESS_IFACE)


class SugarAdhocView(CanvasPulsingIcon):
    """To mimic the mesh behavior on devices where mesh hardware is
    not available we support the creation of an Ad-hoc network on
    three channels 1, 6, 11. This is the class for an icon
    representing a channel in the neighborhood view.

    """

    _ICON_NAME = 'network-adhoc-'
    _NAME = 'Ad-hoc Network '

    def __init__(self, channel):
        CanvasPulsingIcon.__init__(self,
                                   icon_name=self._ICON_NAME + str(channel),
                                   size=style.STANDARD_ICON_SIZE, cache=True)
        self._bus = dbus.SystemBus()
        self._channel = channel
        self._disconnect_item = None
        self._connect_item = None
        self._palette_icon = None
        self._greyed_out = False

        get_adhoc_manager_instance().connect('members-changed',
                                             self.__members_changed_cb)
        get_adhoc_manager_instance().connect('state-changed',
                                             self.__state_changed_cb)

        self.connect('button-release-event', self.__button_release_event_cb)

        pulse_color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                         style.COLOR_TRANSPARENT.get_svg()))
        self.props.pulse_color = pulse_color
        self._state_color = XoColor('%s,%s' % \
                                       (profile.get_color().get_stroke_color(),
                                        style.COLOR_TRANSPARENT.get_svg()))
        self.props.base_color = self._state_color
        self._palette = self._create_palette()
        self.set_palette(self._palette)
        self._palette_icon.props.xo_color = self._state_color

    def _create_palette(self):
        self._palette_icon = Icon( \
                icon_name=self._ICON_NAME + str(self._channel),
                icon_size=style.STANDARD_ICON_SIZE)

        palette_ = palette.Palette(_('Ad-hoc Network %d') % self._channel,
                                   icon=self._palette_icon)

        self._connect_item = MenuItem(_('Connect'), 'dialog-ok')
        self._connect_item.connect('activate', self.__connect_activate_cb)
        palette_.menu.append(self._connect_item)

        self._disconnect_item = MenuItem(_('Disconnect'), 'media-eject')
        self._disconnect_item.connect('activate',
                                      self.__disconnect_activate_cb)
        palette_.menu.append(self._disconnect_item)

        return palette_

    def __button_release_event_cb(self, icon, event):
        get_adhoc_manager_instance().activate_channel(self._channel)

    def __connect_activate_cb(self, icon):
        get_adhoc_manager_instance().activate_channel(self._channel)

    def __disconnect_activate_cb(self, icon):
        get_adhoc_manager_instance().deactivate_active_channel()

    def __state_changed_cb(self, adhoc_manager, channel, device_state):
        if self._channel == channel:
            state = device_state
        else:
            state = network.DEVICE_STATE_UNKNOWN

        if state == network.DEVICE_STATE_ACTIVATED:
            icon_name = '%s-connected' % (self._ICON_NAME + str(self._channel))
        else:
            icon_name = self._ICON_NAME + str(self._channel)

        self.props.base_color = self._state_color
        self._palette_icon.props.xo_color = self._state_color

        if icon_name is not None:
            self.props.icon_name = icon_name
            icon = self._palette.props.icon
            icon.props.icon_name = icon_name

        if state in [network.DEVICE_STATE_PREPARE,
                     network.DEVICE_STATE_CONFIG,
                     network.DEVICE_STATE_NEED_AUTH,
                     network.DEVICE_STATE_IP_CONFIG]:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connecting...')
            self.props.pulsing = True
        elif state == network.DEVICE_STATE_ACTIVATED:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connected')
            self.props.pulsing = False
        else:
            if self._disconnect_item:
                self._disconnect_item.hide()
            self._connect_item.show()
            self._palette.props.secondary_text = None
            self.props.pulsing = False

    def _update_color(self):
        if self._greyed_out:
            self.props.base_color = XoColor('#D5D5D5,#D5D5D5')
        else:
            self.props.base_color = self._state_color

    def __members_changed_cb(self, adhoc_manager, channel, has_members):
        if channel == self._channel:
            if has_members == True:
                self._state_color = profile.get_color()
                self.props.base_color = self._state_color
                self._palette_icon.props.xo_color = self._state_color
            else:
                color = '%s,%s' % (profile.get_color().get_stroke_color(),
                                   style.COLOR_TRANSPARENT.get_svg())
                self._state_color = XoColor(color)
                self.props.base_color = self._state_color
                self._palette_icon.props.xo_color = self._state_color

    def set_filter(self, query):
        name = self._NAME + str(self._channel)
        self._greyed_out = name.lower().find(query) == -1
        self._update_color()


class OlpcMeshView(CanvasPulsingIcon):
    def __init__(self, mesh_mgr, channel):
        CanvasPulsingIcon.__init__(self, icon_name=_OLPC_MESH_ICON_NAME,
                                   size=style.STANDARD_ICON_SIZE, cache=True)
        self._bus = dbus.SystemBus()
        self._channel = channel
        self._mesh_mgr = mesh_mgr
        self._disconnect_item = None
        self._connect_item = None
        self._greyed_out = False
        self._name = ''
        self._device_state = None
        self._active = False
        device = mesh_mgr.mesh_device

        self.connect('button-release-event', self.__button_release_event_cb)

        interface_props = dbus.Interface(device, dbus.PROPERTIES_IFACE)
        interface_props.Get(_NM_DEVICE_IFACE, 'State',
                            reply_handler=self.__get_device_state_reply_cb,
                            error_handler=self.__get_device_state_error_cb)
        interface_props.Get(_NM_OLPC_MESH_IFACE, 'ActiveChannel',
                            reply_handler=self.__get_active_channel_reply_cb,
                            error_handler=self.__get_active_channel_error_cb)

        self._bus.add_signal_receiver(self.__device_state_changed_cb,
                                      signal_name='StateChanged',
                                      path=device.object_path,
                                      dbus_interface=_NM_DEVICE_IFACE)
        self._bus.add_signal_receiver(self.__wireless_properties_changed_cb,
                                      signal_name='PropertiesChanged',
                                      path=device.object_path,
                                      dbus_interface=_NM_OLPC_MESH_IFACE)

        pulse_color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                         style.COLOR_TRANSPARENT.get_svg()))
        self.props.pulse_color = pulse_color
        self.props.base_color = profile.get_color()
        self._palette = self._create_palette()
        self.set_palette(self._palette)

    def _create_palette(self):
        _palette = palette.Palette(_('Mesh Network %d') % self._channel)

        self._connect_item = MenuItem(_('Connect'), 'dialog-ok')
        self._connect_item.connect('activate', self.__connect_activate_cb)
        _palette.menu.append(self._connect_item)

        return _palette

    def __get_device_state_reply_cb(self, state):
        self._device_state = state
        self._update()

    def __get_device_state_error_cb(self, err):
        logging.error('Error getting the device state: %s', err)

    def __device_state_changed_cb(self, new_state, old_state, reason):
        self._device_state = new_state
        self._update()

    def __get_active_channel_reply_cb(self, channel):
        self._active = (channel == self._channel)
        self._update()

    def __get_active_channel_error_cb(self, err):
        logging.error('Error getting the active channel: %s', err)

    def __wireless_properties_changed_cb(self, properties):
        if 'ActiveChannel' in properties:
            channel = properties['ActiveChannel']
            self._active = (channel == self._channel)
            self._update()

    def _update(self):
        if self._active:
            state = self._device_state
        else:
            state = network.DEVICE_STATE_UNKNOWN

        if state in [network.DEVICE_STATE_PREPARE,
                     network.DEVICE_STATE_CONFIG,
                     network.DEVICE_STATE_NEED_AUTH,
                     network.DEVICE_STATE_IP_CONFIG]:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connecting...')
            self.props.pulsing = True
        elif state == network.DEVICE_STATE_ACTIVATED:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connected')
            self.props.pulsing = False
        else:
            if self._disconnect_item:
                self._disconnect_item.hide()
            self._connect_item.show()
            self._palette.props.secondary_text = None
            self.props.pulsing = False

    def _update_color(self):
        if self._greyed_out:
            self.props.base_color = XoColor('#D5D5D5,#D5D5D5')
        else:
            self.props.base_color = profile.get_color()

    def __connect_activate_cb(self, icon):
        self._connect()

    def __button_release_event_cb(self, icon, event):
        self._connect()

    def _connect(self):
        self._mesh_mgr.user_activate_channel(self._channel)

    def __activate_reply_cb(self, connection):
        logging.debug('Connection activated: %s', connection)

    def __activate_error_cb(self, err):
        logging.error('Failed to activate connection: %s', err)

    def set_filter(self, query):
        self._greyed_out = (query != '')
        self._update_color()

    def disconnect(self):
        device_object_path = self._mesh_mgr.mesh_device.object_path

        self._bus.remove_signal_receiver(self.__device_state_changed_cb,
                                         signal_name='StateChanged',
                                         path=device_object_path,
                                         dbus_interface=_NM_DEVICE_IFACE)
        self._bus.remove_signal_receiver(self.__wireless_properties_changed_cb,
                                         signal_name='PropertiesChanged',
                                         path=device_object_path,
                                         dbus_interface=_NM_OLPC_MESH_IFACE)

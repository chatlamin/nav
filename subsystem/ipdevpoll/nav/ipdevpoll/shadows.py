# -*- coding: utf-8 -*-
#
# Copyright (C) 2009, 2010 UNINETT AS
#
# This file is part of Network Administration Visualized (NAV).
#
# NAV is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.  You should have received a copy of the GNU General Public
# License along with NAV. If not, see <http://www.gnu.org/licenses/>.
#
"""Shadow model classes.

This module defines Shadow classes for use in ipdevpoll's storage system.  A
Shadow object will mimic a Django model object, but will not be a "live
object", in the sense that access to member attributes will not result in
database I/O.

"""
import datetime
import IPy

from django.db.models import Q
from django.db import transaction

from nav.models import manage, oid
from storage import Shadow
import descrparsers
import utils

# Shadow classes.  Not all of these will be used to store data, but
# may be used to retrieve and cache existing database records.

class Netbox(Shadow):
    __shadowclass__ = manage.Netbox
    __lookups__ = ['sysname', 'ip']

    def prepare(self, containers):
        """Attempts to solve serial number conflicts before savetime.

        Specifically, if another Netbox in the database is registered with the
        same serial number as this one, we empty this one's serial number to
        avoid db integrity conflicts.

        """
        if self.device and self.device.serial:
            try:
                other = manage.Netbox.objects.get(
                    device__serial=self.device.serial)
            except manage.Netbox.DoesNotExist:
                pass
            else:
                if other.id != self.id:
                    self._logger.warning(
                        "Serial number conflict, attempting peaceful "
                        "resolution (%s): "
                        "%s [%s] (id: %s) <-> %s [%s] (id: %s)",
                        self.device.serial, 
                        self.sysname, self.ip, self.id,
                        other.sysname, other.ip, other.id)
                    self.device.serial = None

    @classmethod
    @transaction.commit_manually
    def cleanup_replaced_netbox(cls, netbox_id, new_type):
        """Removes basic inventory knowledge for a netbox.

        When a netbox has changed type (sysObjectID), this can be called to set
        its new type, delete its modules and interfaces, and reset its
        up_to_date status.

        Arguments:

            netbox_id -- Netbox primary key integer.
            new_type -- A NetboxType shadow container representing the new
                        type.

        """
        try:
            type_ = new_type.convert_to_model()
            if type_:
                type_.save()

            netbox = manage.Netbox.objects.get(id=netbox_id)
            cls._logger.warn("Removing stored inventory info for %s",
                             netbox.sysname)
            netbox.type = type_
            netbox.up_to_date = False
            netbox.save()

            netbox.module_set.all().delete()
            netbox.interface_set.all().delete()
        except:
            cls._logger.exception("cleanup_replaced_netbox: unhandled "
                                  "exception")
            transaction.rollback()
            raise
        else:
            transaction.commit()


class NetboxType(Shadow):
    __shadowclass__ = manage.NetboxType
    __lookups__ = ['sysobjectid']

class NetboxInfo(Shadow):
    __shadowclass__ = manage.NetboxInfo
    __lookups__ = [('netbox', 'key', 'variable')]

class Vendor(Shadow):
    __shadowclass__ = manage.Vendor

class Module(Shadow):
    __shadowclass__ = manage.Module
    __lookups__ = [('netbox', 'name'), 'device']

    def _fix_binary_garbage(self):
        """Fixes string attributes that appear as binary garbage."""

        if utils.is_invalid_utf8(self.model):
            self._logger.warn("Invalid value for model: %r", self.model)
            self.model = repr(self.model)
        
    def prepare(self, containers):
        self._fix_binary_garbage()

    @classmethod
    def _make_modulestate_event(cls, django_module):
        from nav.models.event import EventQueue as Event
        e = Event()
        # FIXME: ipdevpoll is not a registered subsystem in the database yet
        e.source_id = 'getDeviceData'
        e.target_id = 'eventEngine'
        e.device = django_module.device
        e.netbox = django_module.netbox
        e.event_type_id = 'moduleState'
        return e

    @classmethod
    def _dispatch_down_event(cls, django_module):
        e = cls._make_modulestate_event(django_module)
        e.state = e.STATE_START
        e.save()

    @classmethod
    def _dispatch_up_event(cls, django_module):
        e = cls._make_modulestate_event(django_module)
        e.state = e.STATE_END
        e.save()

    @classmethod
    def _handle_missing_modules(cls, containers):
        """Handles modules that have gone missing from a device."""
        netbox = containers.get(None, Netbox)
        all_modules = manage.Module.objects.filter(netbox__id = netbox.id)
        modules_up = all_modules.filter(up=manage.Module.UP_UP)
        modules_down = all_modules.filter(up=manage.Module.UP_DOWN)

        collected_modules = containers[Module].values()
        collected_module_pks = [m.id for m in collected_modules if m.id]

        missing_modules = modules_up.exclude(id__in=collected_module_pks)
        reappeared_modules = modules_down.filter(id__in=collected_module_pks)

        if missing_modules:
            shortlist = ", ".join(m.name for m in missing_modules)
            cls._logger.info("%d modules went missing on %s (%s)",
                             netbox.sysname, len(missing_modules), shortlist)
            for module in missing_modules:
                cls._dispatch_down_event(module)

        if reappeared_modules:
            shortlist = ", ".join(m.name for m in reappeared_modules)
            cls._logger.info("%d modules reappeared on %s (%s)",
                             netbox.sysname, len(reappeared_modules),
                             shortlist)
            for module in reappeared_modules:
                cls._dispatch_up_event(module)


    @classmethod
    def cleanup_after_save(cls, containers):
        cls._handle_missing_modules(containers)
        return super(Module, cls).cleanup_after_save(containers)


class Device(Shadow):
    __shadowclass__ = manage.Device
    __lookups__ = ['serial']

    def _fix_binary_garbage(self):
        """Fixes version strings that appear as binary garbage."""

        for attr in ('hardware_version',
                     'software_version',
                     'firmware_version',
                     'serial'):
            value = getattr(self, attr)
            if utils.is_invalid_utf8(value):
                self._logger.warn("Invalid value for %s: %r",
                                  attr, value)
                setattr(self, attr, repr(value))
        
    def prepare(self, containers):
        self._fix_binary_garbage()

class Interface(Shadow):
    __shadowclass__ = manage.Interface

    @classmethod
    def _mark_missing_interfaces(cls, containers):
        """Marks interfaces in db as gone if they haven't been collected in
        this round.

        This is designed to run in the cleanup_after_save phase, as it needs
        primary keys of the containers to have been found.

        TODO: Make a deletion algorithm.  Missing interfaces that do
        not correspond to a module known to be down should be deleted.
        If all interfaces belonging to a specific module is down, we
        may have detected that the module is down as well.

        """
        netbox = containers.get(None, Netbox)
        found_interfaces = containers[cls].values()
        timestamp = datetime.datetime.now()

        # start by finding the existing interface's primary keys
        pks = [i.id for i in found_interfaces if i.id]

        # the rest of the interfaces that haven't already been marked as gone,
        # should be marked as such
        missing_interfaces = manage.Interface.objects.filter(
            netbox=netbox.id, gone_since__isnull=True
            ).exclude(pk__in=pks)

        count = missing_interfaces.count()
        if count > 0:
            cls._logger.debug("_mark_missing_interfaces(%s): "
                              "marking %d interfaces as gone",
                              netbox.sysname, count)
        missing_interfaces.update(gone_since=timestamp)

    @classmethod
    def _delete_missing_interfaces(cls, containers):
        """Deletes missing interfaces from the database."""
        netbox = containers.get(None, Netbox)
        base_query = manage.Interface.objects.filter(
            netbox__id = netbox.id)

        missing_ifs = base_query.exclude(
            gone_since__isnull=True).values('pk', 'ifindex')

        # at this time, we only want to delete those gone_interface who appear
        # to have ifindex duplicates that aren't missing.
        deleteable = []
        for missing_if in missing_ifs:
            dupes = base_query.filter(
                ifindex=missing_if['ifindex'], gone_since__isnull=True)
            if dupes.count() > 0:
                deleteable.append(missing_if['pk'])

        if deleteable:
            cls._logger.info("(%s) Deleting %d missing interfaces",
                             netbox.sysname, len(deleteable))
            manage.Interface.objects.filter(pk__in=deleteable).delete()


    @classmethod
    def cleanup_after_save(cls, containers):
        """Cleans up Interface data."""
        cls._mark_missing_interfaces(containers)
        cls._delete_missing_interfaces(containers)
        super(Interface, cls).cleanup_after_save(containers)

    def lookup_matching_objects(self, containers):
        """Finds existing db objects that match this container.

        ifName is a more important identifier than ifindex, as ifindexes may
        change at any time.  A database migrated from NAV 3.5 may also have a
        lot of weird or duplicate data, due to sloppiness on getDeviceData's
        part.

        """
        query = manage.Interface.objects.filter(netbox__id=self.netbox.id)
        result = None
        if self.ifname:
            result = query.filter(ifname=self.ifname)
        if not result and self.ifdescr:
            # this is only likely on a db recently migrated from NAV 3.5
            result = query.filter(ifname=self.ifdescr,
                                  ifdescr=self.ifdescr)
        if len(result) > 1:
            # Multiple ports with same name? damn...
            # also filter for ifindex, maybe we get lucky
            result = result.filter(ifindex=self.ifindex)

        # If none of this voodoo helped, try matching ifindex only
        if not result:
            result = query.filter(ifindex=self.ifindex)

        return result

    def get_existing_model(self, containers):
        """Implements custom logic for finding known interfaces."""
        result = self.lookup_matching_objects(containers)
        if not result:
            return None
        elif len(result) > 1:
            self._logger.debug(
                "get_existing_model: multiple matching objects returned. "
                "query is: %r", result.query.as_sql())
            raise manage.Interface.MultipleObjectsReturned(
                "get_existing_model: "
                "Found multiple matching objects for %r" % self)
        else:
            self.id = result[0].id
            return result[0]

class Location(Shadow):
    __shadowclass__ = manage.Location

class Room(Shadow):
    __shadowclass__ = manage.Room

class Category(Shadow):
    __shadowclass__ = manage.Category

class Organization(Shadow):
    __shadowclass__ = manage.Organization

class Usage(Shadow):
    __shadowclass__ = manage.Usage

class Vlan(Shadow):
    __shadowclass__ = manage.Vlan

    def _get_my_prefixes(self, containers):
        """Get a list of Prefix shadow objects that point to this Vlan."""
        if Prefix in containers:
            all_prefixes = containers[Prefix].values()
            my_prefixes = [prefix for prefix in all_prefixes
                           if prefix.vlan is self]
            return my_prefixes
        else:
            return []

    def _get_vlan_from_my_prefixes(self, containers):
        """Find and return an existing vlan any shadow prefix object pointing
        to this Vlan.

        """
        my_prefixes = self._get_my_prefixes(containers)
        for prefix in my_prefixes:
            live_prefix = prefix.get_existing_model()
            if live_prefix and live_prefix.vlan_id:
                # We just care about the first associated prefix we found
                self._logger.debug(
                    "_get_vlan_from_my_prefixes: selected prefix "
                    "%s for possible vlan match for %r (%s), "
                    "pre-existing is %r",
                    live_prefix.net_address, self, id(self),
                    live_prefix.vlan)
                return live_prefix.vlan

    def get_existing_model(self, containers):
        """Finds pre-existing Vlan object using custom logic.

        This is complicated because of the relationship between Prefix and
        Vlan, and the fact that multiple vlans with the same vlan number may
        exist, and even Vlan entries without a number.

        If we have a known netident and find an existing record with the same
        vlan value (either a number or NULL) and netident, they are considered
        the same.

        Otherwise, we consider the prefixes that are associated with this vlan.
        If these prefixes already exist in the database, they are likely
        connected to the existing vlan object that we should update.

        If all else fails, a new record is created.

        """
        # Only lookup if primary key isn't already set.
        if self.id:
            return super(Vlan, self).get_existing_model(containers)

        if self.net_ident:
            vlans = manage.Vlan.objects.filter(vlan=self.vlan,
                                               net_ident=self.net_ident)
            if vlans:
                self._logger.debug(
                    "get_existing_model: %d matches found for "
                    "vlan+net_ident: %r",
                    len(vlans), self)
                return vlans[0]

        vlan = self._get_vlan_from_my_prefixes(containers)
        if vlan:
            # Only claim to be the same Vlan object if the vlan number is the
            # same, or the pre-existing object has no Vlan number.
            if vlan.vlan is None or vlan.vlan == self.vlan:
                return vlan

    def _guesstimate_net_type(self, containers):
        """Guesstimates a net type for this VLAN, based on its prefixes.

        Various algorithms may be used (and the database may be queried).

        Returns:

          A NetType storage container, suitable for assignment to
          Vlan.net_type.

        """
        prefix_containers = self._get_my_prefixes(containers)
        # ATM we only look at the first prefix we can find.
        if prefix_containers:
            prefix = IPy.IP(prefix_containers[0].net_address)
        else:
            return NetType.get('unknown')

        net_type = 'lan'
        # Get the number of router ports attached to this prefix
        port_count = manage.GwPortPrefix.objects.filter(
            prefix__net_address=str(prefix),
            interface__netbox__category__id__in=('GSW', 'GW')).count()

        if prefix.version() == 6 and prefix.prefixlen() == 128:
            net_type = 'loopback'
        elif prefix.version() == 4:
            if prefix.prefixlen() == 32:
                net_type = 'loopback'
            elif prefix.prefixlen() == 30:
                net_type = port_count == 1 and 'elink' or 'link'
        if port_count > 2:
            net_type = 'core'
        elif port_count == 2:
            net_type = 'link'

        return NetType.get(net_type)

    def prepare(self, containers):
        """Prepares this VLAN object for saving.

        The data stored in a VLAN object consists much of what can be found
        from other objects, such as interfaces and prefixes, so the logic in
        here can becore rather involved.

        """
        if not self.net_type or self.net_type.id == 'unknown':
            net_type = self._guesstimate_net_type(containers)
            if net_type:
                self.net_type = net_type

class Prefix(Shadow):
    __shadowclass__ = manage.Prefix
    __lookups__ = [('net_address', 'vlan'), 'net_address']

    @classmethod
    def _delete_unused_prefixes(cls):
        """Deletes prefixes that appear to have fallen into disuse.

        A disused prefix is one not attached to any gwport and not attached to
        any vlan that is in use somewhere.

        """
        keep_vlans = manage.Vlan.objects.filter(
            Q(net_type='scope') | Q(swportvlan__isnull=False))
        keep_vlans_q = keep_vlans.values('pk').query

        unrouted_prefixes = manage.Prefix.objects.exclude(gwportprefix__isnull=False)
        deleteable_prefixes = unrouted_prefixes.exclude(vlan__in=keep_vlans_q)

        count = len(deleteable_prefixes)
        deleteable_prefixes.delete()
        if count:
            cls._logger.info("Deleted %d unused prefixes", count)


    @classmethod
    def _delete_unused_vlans(cls):
        """Deletes vlans that appear to have fallen into disuse.

        A disused vlan is one not attached to any prefix, to any swport and is
        not a scope type vlan.

        """
        deleteable_vlans = manage.Vlan.objects.exclude(
            Q(net_type='scope') | Q(swportvlan__isnull=False) |
            Q(prefix__isnull=False))

        count = len(deleteable_vlans)
        deleteable_vlans.delete()
        if count:
            cls._logger.info("Deleted %d unused VLANs", count)

    @classmethod
    def cleanup_after_save(cls, containers):
        """Deletes unused vlans and prefixes from the database.

        TODO: This could possibly be more suitable as a database trigger!

        """
        cls._delete_unused_prefixes()
        cls._delete_unused_vlans()
        super(Prefix, cls).cleanup_after_save(containers)


class GwPortPrefix(Shadow):
    __shadowclass__ = manage.GwPortPrefix
    __lookups__ = ['gw_ip']

    def _parse_description(self, containers):
        """Parses router port descriptions to find a suitable Organization,
        netident, usageid and description for this vlan.

        """
        if (not self.interface or \
            not self.interface.netbox or \
            not self.interface.ifalias or \
            not self.prefix or \
            not self.prefix.vlan
            ): 
            return

        sysname = self.interface.netbox.sysname
        ifalias = self.interface.ifalias
        vlan = self.prefix.vlan
        for parse in (descrparsers.parse_ntnu_convention,
                      descrparsers.parse_uninett_convention):
            data = parse(sysname, ifalias)
            if data:
                break
        if not data:
            self._logger.info("ifalias did not match any known router port "
                              "description conventions: %s", ifalias)
            vlan.netident = ifalias
            return

        if data.get('net_type', None):
            vlan.net_type = NetType.get(data['net_type'])
        if data.get('netident', None):
            vlan.net_ident = data['netident']
        if data.get('usage', None):
            vlan.usage = containers.factory(data['usage'], Usage)
            vlan.usage.id = data['usage']
        if data.get('comment', None):
            vlan.description = data['comment']
        if data.get('org', None):
            vlan.organization = containers.factory(data['org'], Organization)
            vlan.organization.id = data['org']

    def prepare(self, containers):
        self._parse_description(containers)

class NetType(Shadow):
    __shadowclass__ = manage.NetType

    @classmethod
    def get(cls, net_type_id):
        """Creates a NetType container for the given net_type id."""
        n = cls()
        n.id = net_type_id
        return n

 
class SwPortVlan(Shadow):
    __shadowclass__ = manage.SwPortVlan

class Arp(Shadow):
    __shadowclass__ = manage.Arp
    __lookups__ = [('netbox', 'ip', 'mac', 'end_time')]

class Cam(Shadow):
    __shadowclass__ = manage.Cam
    __lookups__ = [('netbox', 'ifindex', 'mac', 'miss_count')]

class SwPortAllowedVlan(Shadow):
    __shadowclass__ = manage.SwPortAllowedVlan
    __lookups__ = ['interface']

class SnmpOid(Shadow):
    __shadowclass__ = oid.SnmpOid
    __lookups__ = ['oidkey']

class NetboxSnmpOid(Shadow):
    __shadowclass__ = oid.NetboxSnmpOid



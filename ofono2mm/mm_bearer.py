import asyncio

from dbus_fast.service import ServiceInterface, method, dbus_property
from dbus_fast.constants import PropertyAccess
from dbus_fast import Variant

from ofono2mm.utils import async_retryable, save_setting, read_setting
from ofono2mm.logging import ofono2mm_print

class MMBearerInterface(ServiceInterface):
    def __init__(self, ofono_client, modem_name, ofono_interfaces, ofono_interface_props, mm_modem, verbose=False):
        super().__init__('org.freedesktop.ModemManager1.Bearer')
        self.modem_name = modem_name
        ofono2mm_print("Initializing Bearer interface", verbose)
        self.ofono_client = ofono_client
        self.ofono_proxy = self.ofono_client["ofono_modem"][modem_name]
        self.ofono_interfaces = ofono_interfaces
        self.ofono_interface_props = ofono_interface_props
        self.mm_modem = mm_modem
        self.verbose = verbose
        self.disconnecting = False
        self.reconnect_task = None
        self.ofono_ctx = None
        self.active_connect = 0
        self.own_object_path = None
        self.props = {
            "Interface": Variant('s', ''),
            "Connected": Variant('b', False),
            "Suspended": Variant('b', False),
            "Multiplexed": Variant('b', True),
            "Ip4Config": Variant('a{sv}', {
                "method": Variant('u', 3) # on runtime dhcp MM_BEARER_IP_METHOD_DHCP
            }),
            "Ip6Config": Variant('a{sv}', {
                "method": Variant('u', 3) # on runtime dhcp MM_BEARER_IP_METHOD_DHCP
            }),
            "ReloadStatsSupported": Variant('b', False),
            "IpTimeout": Variant('u', 0),
            "BearerType": Variant('u', 1),
            "Properties": Variant('a{sv}', {
                "apn": Variant('s', ''),
                "ip-type": Variant('u', 1), # hardcoded value ipv4 MM_BEARER_IP_FAMILY_IPV4
                "apn-type": Variant('u', 2), # hardcoded value default internet MM_BEARER_APN_TYPE_DEFAULT
                "allowed-auth": Variant('u', 0), # on runtime unknown MM_BEARER_ALLOWED_AUTH_UNKNOWN
                "user": Variant('s', ''),
                "password": Variant('s', ''),
                "access-type-preference": Variant('u', 0), # on runtime none MM_BEARER_ACCESS_TYPE_PREFERENCE_NONE
                "roaming-allowance": Variant('u', 0), # on runtime none MM_BEARER_ROAMING_ALLOWANCE_NONE
                "profile-id": Variant('i', -1),
                "profile-name": Variant('s', ''),
                "profile-enabled": Variant('b', True),
                "profile-source": Variant('u', 0), # hardcoded value unknown MM_BEARER_PROFILE_SOURCE_UNKNOWN
            })
        }

    @dbus_property(access=PropertyAccess.READ)
    def Interface(self) -> 's':
        return self.props['Interface'].value

    @dbus_property(access=PropertyAccess.READ)
    def Connected(self) -> 'b':
        return self.props['Connected'].value

    @dbus_property(access=PropertyAccess.READ)
    def Suspended(self) -> 'b':
        return self.props['Suspended'].value

    @dbus_property(access=PropertyAccess.READ)
    def Multiplexed(self) -> 'b':
        return self.props['Multiplexed'].value

    @dbus_property(access=PropertyAccess.READ)
    def Ip4Config(self) -> 'a{sv}':
        return self.props['Ip4Config'].value

    @dbus_property(access=PropertyAccess.READ)
    def Ip6Config(self) -> 'a{sv}':
        return self.props['Ip6Config'].value

    @dbus_property(access=PropertyAccess.READ)
    def ReloadStatsSupported(self) -> 'b':
        return self.props['ReloadStatsSupported'].value

    @dbus_property(access=PropertyAccess.READ)
    def IpTimeout(self) -> 'u':
        return self.props['IpTimeout'].value

    @dbus_property(access=PropertyAccess.READ)
    def BearerType(self) -> 'u':
        return self.props['BearerType'].value

    @dbus_property(access=PropertyAccess.READ)
    def Properties(self) -> 'a{sv}':
        return self.props['Properties'].value

    async def set_props(self):
        ofono2mm_print("Setting properties", self.verbose)

        old_props = self.props

        if 'org.ofono.ConnectionManager' in self.ofono_interface_props:
            # GetContexts can take a few seconds to come up. If we fail with a DBusError, we'll just wait a bit and try again.
            retries_left = 5
            while retries_left > 0:
                try:
                    contexts = await self.ofono_proxy['org.ofono.ConnectionManager'].call_get_contexts()
                    break
                except Exception as e:
                    retries_left -= 1
                    if retries_left == 0:
                        ofono2mm_print(f"Failed to get contexts: {e}", self.verbose)
                        return
                    else:
                        await asyncio.sleep(0.2)

            chosen_apn = ''
            chosen_auth_method = ''
            chosen_username = ''
            chosen_password = ''
            for ctx in contexts:
                name = ctx[1].get('Type', Variant('s', '')).value
                if name.lower() == "internet":
                    apn = ctx[1].get('AccessPointName', Variant('s', '')).value
                    auth_method = ctx[1].get('AuthenticationMethod', Variant('s', '')).value
                    username = ctx[1].get('Username', Variant('s', '')).value
                    password = ctx[1].get('Password', Variant('s', '')).value
                    if apn:
                        chosen_apn = apn
                        chosen_auth_method = auth_method
                        chosen_username = username
                        chosen_password = password

            self.props['Properties'].value['apn'] = Variant('s', chosen_apn)
            self.props['Properties'].value['user'] = Variant('s', chosen_username)
            self.props['Properties'].value['password'] = Variant('s', chosen_password)

            if chosen_auth_method == 'none':
                self.props['Properties'].value['allowed-auth'] = Variant('u', 1) # none MM_BEARER_ALLOWED_AUTH_NONE
            elif chosen_auth_method == 'pap':
                self.props['Properties'].value['allowed-auth'] = Variant('u', 2) # pap MM_BEARER_ALLOWED_AUTH_PAP
            elif chosen_auth_method == 'chap':
                self.props['Properties'].value['allowed-auth'] = Variant('u', 3) # chap MM_BEARER_ALLOWED_AUTH_CHAP
            else:
                self.props['Properties'].value['allowed-auth'] = Variant('u', 0) # unknown MM_BEARER_ALLOWED_AUTH_UNKNOWN

            ofono_interface = self.ofono_client["ofono_modem"][self.modem_name]['org.ofono.ConnectionManager']

            roaming_allowed = None
            ofono_props = await ofono_interface.call_get_properties()

            if ofono_props.get('RoamingAllowed', Variant('b', True).value) != "":
                roaming_allowed = ofono_props.get('RoamingAllowed', Variant('b', True).value).value

                if read_setting('roaming').strip() != str(roaming_allowed):
                    ofono2mm_print("Saving roaming toggle state", self.verbose)
                    save_setting('roaming', str(roaming_allowed))

                if roaming_allowed == True:
                    self.props['Properties'].value['roaming-allowance'] = Variant('u', 2) # roaming partner network MM_BEARER_ROAMING_ALLOWANCE_PARTNER
                elif roaming_allowed == False:
                    self.props['Properties'].value['roaming-allowance'] = Variant('u', 0) # roaming none MM_BEARER_ROAMING_ALLOWANCE_NONE

        for prop in self.props:
            if self.props[prop].value != old_props[prop].value:
                self.emit_properties_changed({prop: self.props[prop].value})

    @method()
    async def Connect(self):
        ofono2mm_print("Called bearer connect", self.verbose)
        self.active_connect += 1
        await self.doConnect()

    @async_retryable()
    async def doConnect(self):
        ofono2mm_print(f"Connecting the bearer at path {self.own_object_path} with ofono context {self.ofono_ctx}", self.verbose)
        try:
            await self.set_props()
        except Exception as e:
            ofono2mm_print(f"Failed to set props: {e}", self.verbose)

        ofono_ctx_interface = self.ofono_client["ofono_context"][self.ofono_ctx]['org.ofono.ConnectionContext']
        ofono2mm_print(f"Number of active connection requests: {self.active_connect}", self.verbose)

        try:
            await asyncio.wait_for(ofono_ctx_interface.call_set_property("Active", Variant('b', True)), timeout=5.0)
        except Exception as e:
            if "GPRS" in str(e):
                # no signal? wait a litle and try again
                ofono2mm_print(f"Failed to set context to active: {e}", self.verbose)
                await asyncio.sleep(5)
                raise Exception(str(e))

        if self.active_connect >= 1:
            self.active_connect -= 1

        # Clear the reconnection task
        self.reconnect_task = None
        ofono2mm_print(f"Number of active connection requests: {self.active_connect}", self.verbose)

    @method()
    async def Disconnect(self):
        ofono2mm_print("Called bearer disconnect", self.verbose)
        await self.doDisconnect()

    async def cancel_reconnect_task(self):
        if self.reconnect_task is not None:
            self.reconnect_task.cancel()
            try:
                await self.reconnect_task
            except asyncio.CancelledError:
                # Finally
                pass
            finally:
                self.reconnect_task = None

    async def doDisconnect(self):
        ofono2mm_print(f"Disconnecting the bearer at path {self.own_object_path} with ofono context {self.ofono_ctx}", self.verbose)
        self.disconnecting = True

        # Cancel an eventual reconnection task
        await self.cancel_reconnect_task()

        ofono_ctx_interface = self.ofono_client["ofono_context"][self.ofono_ctx]['org.ofono.ConnectionContext']
        await ofono_ctx_interface.call_set_property("Active", Variant('b', False))

    async def add_auth_ofono(self, username, password):
        ofono2mm_print(f"Add authentication to oFono with username '{username}' and password '{password}'", self.verbose)

        ofono_ctx_interface = self.ofono_client["ofono_context"][self.ofono_ctx]['org.ofono.ConnectionContext']
        try:
            await ofono_ctx_interface.call_set_property("Username", Variant('s', username))
            await ofono_ctx_interface.call_set_property("Password", Variant('s', password))
        except Exception as e:
            ofono2mm_print(f"Failed to set ofono authentication: {e}", self.verbose)

    def ofono_context_changed(self, propname, value):
        ofono2mm_print(f"oFono context changed for prop name {propname} set to value {value}", self.verbose)

        if propname == "Active":
            if self.disconnecting and value.value:
                self.disconnecting = False
            elif not self.disconnecting and (not value.value) and self.reconnect_task is None and self.props['Connected'].value:
                # Initiate a reconnection using NetworkManager.
                # TODO: ideally NM would take care of this itself once it learns that we lost the connection.

                self.reconnect_task = asyncio.create_task(self.mm_modem.mm_modem_simple_interface.network_manager_set_apn(force=True))

            self.props['Connected'] = value
            self.emit_properties_changed({'Connected': value.value})
        elif propname == "Settings":
            if 'Interface' in value.value:
                self.props['Interface'] = value.value['Interface']
                self.emit_properties_changed({'Interface': value.value['Interface'].value})
                if [value.value['Interface'].value, 2] not in self.mm_modem.props['Ports'].value:
                    self.mm_modem.props['Ports'].value.append([value.value['Interface'].value, 2]) # port type AT MM_MODEM_PORT_TYPE_AT
            if 'Method' in value.value:
                if value.value['Method'].value == 'static':
                    self.props['Ip4Config'].value['method'] = Variant('u', 2) # static MM_BEARER_IP_METHOD_STATIC
                if value.value['Method'].value == 'dhcp':
                    self.props['Ip4Config'].value['method'] = Variant('u', 3) # dhcp MM_BEARER_IP_METHOD_DHCP
            if 'Address' in value.value:
                self.props['Ip4Config'].value['address'] = value.value['Address']
            if 'DomainNameServers' in value.value:
                for i in range(0, min(3, len(value.value['DomainNameServers'].value))):
                    self.props['Ip4Config'].value['dns' + str(i + 1)] = Variant('s', value.value['DomainNameServers'].value[i])
            if 'Gateway' in value.value:
                self.props['Ip4Config'].value['gateway'] = value.value['Gateway']

            self.emit_properties_changed({'Ip4Config': self.props['Ip4Config'].value})

    def ofono_changed(self, name, varval):
        asyncio.create_task(self.set_props())

    def ofono_client_changed(self, ofono_client):
        self.ofono_client = ofono_client

    def ofono_interface_changed(self, iface):
        def ch(name, varval):
            if iface in self.ofono_interface_props:
                self.ofono_interface_props[iface][name] = varval
            asyncio.create_task(self.set_props())
        return ch

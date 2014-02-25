#!/usr/bin/python

import os
import sys

from subprocess import check_call

from charmhelpers.contrib.unison import (
    ensure_user,
    get_homedir,
    ssh_authorized_peers,
)

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    log,
    relation_get,
    relation_ids,
    relation_set,
    unit_get,
)

from charmhelpers.core.host import (
    mkdir,
    restart_on_change,
    service_restart
)

from charmhelpers.fetch import (
    apt_install, apt_update
)

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    openstack_upgrade_available,
)

from keystone_utils import (
    add_service_to_keystone,
    determine_packages,
    do_openstack_upgrade,
    ensure_initial_admin,
    migrate_database,
    save_script_rc,
    synchronize_service_credentials,
    register_configs,
    relation_list,
    restart_map,
    CLUSTER_RES,
    KEYSTONE_CONF,
    SSH_USER,
)

from charmhelpers.contrib.hahelpers.cluster import (
    eligible_leader,
    get_hacluster_config,
    is_leader,
)

from charmhelpers.payload.execd import execd_preinstall

hooks = Hooks()
CONFIGS = register_configs()

@hooks.hook()
def install():
    execd_preinstall()
    configure_installation_source(config('openstack-origin'))
    apt_update()
    apt_install(determine_packages(), fatal=True)

@hooks.hook('config-changed')
@restart_on_change(restart_map(), stopstart=True)
def config_changed():
    ensure_user(user=SSH_USER, group='juju_keystone')
    homedir = get_homedir(SSH_USER)
    if not os.path.isdir(homedir):
        mkdir(homedir, SSH_USER, 'juju_keystone', 0775)
    check_call(['chmod', '-R', 'g+wrx', '/var/lib/keystone/'])
    if openstack_upgrade_available('keystone'):
        do_openstack_upgrade(configs=CONFIGS)
        check_call(['chmod', '-R', 'g+wrx', '/var/lib/keystone/'])
    save_script_rc()
    configure_https()
    CONFIGS.write_all()
    service_restart('keystone')
    if eligible_leader(CLUSTER_RES):
        migrate_database()
        ensure_initial_admin(config)
        log('Firing identity_changed hook for all related services.')
        # HTTPS may have been set - so fire all identity relations
        # again
        for r_id in relation_ids('identity-service'):
             for unit in relation_list(r_id):
                 identity_changed(relation_id=r_id,
                                  remote_unit=unit)


@hooks.hook('shared-db-relation-joined')
def db_joined():
    relation_set(database=config('database'),
                 username=config('database-user'),
                 hostname=unit_get('private-address'))


@hooks.hook('shared-db-relation-changed')
@restart_on_change(restart_map())
def db_changed():
    if 'shared-db' not in CONFIGS.complete_contexts():
        log('shared-db relation incomplete. Peer not ready?')
        return
    CONFIGS.write(KEYSTONE_CONF)
    service_restart('keystone')
    if eligible_leader(CLUSTER_RES):
        migrate_database()
        ensure_initial_admin(config)


@hooks.hook('identity-service-relation-joined')
def identity_joined():
    """ Do nothing until we get information about requested service """
    pass


@hooks.hook('identity-service-relation-changed')
@restart_on_change(restart_map())
def identity_changed():
    if not eligible_leader(CLUSTER_RES):
        log('Deferring identity_changed() to service leader.')
    #if 'identity-service' not in CONFIGS.complete_contexts():
    #    return
    if eligible_leader(CLUSTER_RES):
        add_service_to_keystone()
        synchronize_service_credentials()



@hooks.hook('cluster-relation-joined')
def cluster_joined():
    ssh_authorized_peers(user=SSH_USER,
                         group='juju_keystone',
                         peer_interface='cluster',
                         ensure_local_user=True)


@hooks.hook('cluster-relation-changed',
            'cluster-relation-departed')
@restart_on_change(restart_map(), stopstart=True)
def cluster_changed():
    ssh_authorized_peers(user=SSH_USER,
                         group='juju_keystone',
                         peer_interface='cluster',
                         ensure_local_user=True)
    synchronize_service_credentials()
    CONFIGS.write_all()


@hooks.hook('ha-relation-joined')
def ha_joined():
    config = get_hacluster_config()
    resources = {
        'res_ks_vip': 'ocf:heartbeat:IPaddr2',
        'res_ks_haproxy': 'lsb:haproxy',
    }
    vip_params = 'params ip="%s" cidr_netmask="%s" nic="%s"' % \
                 (config['vip'], config['vip_cidr'], config['vip_iface'])
    resource_params = {
        'res_ks_vip': vip_params,
        'res_ks_haproxy': 'op monitor interval="5s"'
    }
    init_services = {
        'res_ks_haproxy': 'haproxy'
    }
    clones = {
        'cl_ks_haproxy': 'res_ks_haproxy'
    }
    relation_set(init_services=init_services,
                 corosync_bindiface=config['ha-bindiface'],
                 corosync_mcastport=config['ha-mcastport'],
                 resources=resources,
                 resource_params=resource_params,
                 clones=clones)


@hooks.hook('ha-relation-changed')
def ha_changed():
    clustered = relation_get('clustered')
    if not clustered or clustered in [None, 'None', '']:
        log('ha_changed: hacluster subordinate not fully clustered.')
        return
    if not is_leader(CLUSTER_RES):
        log('ha_changed: hacluster complete but we are not leader.')
        return
    ensure_initial_admin(config)
    log('Cluster configured, notifying other services and updating '
        'keystone endpoint configuration')
    for rid in relation_ids('identity-service'):
        identity_joined(rid=rid)
    CONFIGS.write_all()


def configure_https():
    '''
    Enables SSL API Apache config if appropriate and kicks identity-service
    with any required api updates.
    '''
    # need to write all to ensure changes to the entire request pipeline
    # propagate (c-api, haprxy, apache)
    CONFIGS.write_all()
    if 'https' in CONFIGS.complete_contexts():
        cmd = ['a2ensite', 'openstack_https_frontend']
        check_call(cmd)
    else:
        cmd = ['a2dissite', 'openstack_https_frontend']
        check_call(cmd)

    for rid in relation_ids('identity-service'):
        identity_joined(rid=rid)


@hooks.hook('upgrade-charm')
def upgrade_charm():
    if openstack_upgrade_available('keystone'):
        do_openstack_upgrade(configs=CONFIGS)
    save_script_rc()
    configure_https()
    CONFIGS.write_all()


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))


if __name__ == '__main__':
    main()

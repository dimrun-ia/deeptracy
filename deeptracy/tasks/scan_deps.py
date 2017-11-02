# Copyright 2017 BBVA
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

from datetime import datetime

import docker
from docker.errors import DockerException

from celery import task
from celery.utils.log import get_task_logger
from deeptracy_core.dal.database import db
from deeptracy_core.dal.scan.manager import get_scan, get_previous_scan_for_project, update_scan_state, ScanState
from deeptracy_core.dal.scan_dep.manager import add_scan_deps, compare_scan_deps

from .start_scan import start_scan

logger = get_task_logger('deeptracy')


@task(name="scan_deps")
def scan_deps(scan_id: str):
    with db.session_scope() as session:
        logger.debug('{} extract dependencies'.format(scan_id))

        scan = get_scan(scan_id, session)

        dependencies = get_dependencies(scan.lang, scan.source_path)
        logger.debug('found dependencies {}'.format(dependencies))

        # save all dependencies in the database
        add_scan_deps(scan.id, dependencies, datetime.now(), session)
        session.commit()
        logger.debug('saved {} dependencies'.format(len(dependencies)))

        # compare the dependencies in this scan with the last scan for this project
        previous_scan = get_previous_scan_for_project(scan.project_id, scan.id, session)

        if previous_scan is None:
            logger.debug('no previous scan found for {}'.format(scan_id))
            deps_equals = False
        else:
            logger.debug('previous scan to {} is {}'.format(scan_id, previous_scan.id))
            deps_equals = compare_scan_deps(scan.id, previous_scan.id, session)

            if deps_equals:
                update_scan_state(scan, ScanState.SAME_DEPS_AS_PREVIOUS, session)
                logger.debug('{} scan has same deps as {}'.format(scan_id, previous_scan.id))

    if not deps_equals:
        start_scan.delay(scan_id)


def get_dependencies(lang: str, sources: str):
    """Given a language and a sources path, scan that sources to get a complete list of dependencies.

    Each language get the dependencies in a different way, but always inside a container to isolate the sources"""

    mounted_vol = '/opt/deeptracy'
    docker_volumes = {
        sources: {
            'bind': mounted_vol,
            'mode': 'rw'
        }
    }

    if lang == 'nodejs':
        return get_dependencies_for_nodejs(sources, mounted_vol, docker_volumes)


def get_dependencies_for_nodejs(sources :str, mounted_vol: str, docker_volumes: dict):
    image = 'node:latest'
    script_contents = '#!/bin/bash \n' \
                      'mkdir /tmp/deeptracy \n' \
                      'cp -R {mounted_vol} /tmp/deeptracy \n' \
                      'cd {mounted_vol} \n' \
                      'npm install --ignore-scripts \n' \
                      'npm ls --parseable --long' \
        .format(
            mounted_vol=mounted_vol
        )

    # create the script that makes the clone
    script = os.path.join(sources, 'get_deps.sh')
    with open(script, "w") as f:
        f.write(script_contents)

    os.system('chmod +x {}'.format(script))
    command = os.path.join(mounted_vol, 'get_deps.sh')  # execute script IGNORING errors

    logger.debug('extract deps with command {}'.format(command))

    docker_client = docker.from_env()

    container = docker_client.containers.run(
        image=image,
        command=command,
        remove=False,
        volumes=docker_volumes,
        detach=True
    )

    dep_list = []
    for lineb in container.logs(stream=True):
        # remove node_modules path from each dep
        line = lineb.decode()
        if 'node_modules/' in line:
            # TODO: deps has paths and need to be parsed carefully
            dep_split = line.split('node_modules/', 1)
            # parsed_dep_list.append(dep_split[-1])
            dep_list.append(dep_split[1])

    return dep_list

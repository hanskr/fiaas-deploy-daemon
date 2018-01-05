#!/usr/bin/env python
# -*- coding: utf-8
from collections import defaultdict

import mock
import pytest
from mock import create_autospec
from requests import Response

from fiaas_deploy_daemon.config import Configuration
from fiaas_deploy_daemon.deployer.kubernetes.deployment import _make_probe, DeploymentDeployer
from fiaas_deploy_daemon.specs.models import CheckSpec, HttpCheckSpec, TcpCheckSpec, PrometheusSpec, AutoscalerSpec, \
    ResourceRequirementSpec, ResourcesSpec, ExecCheckSpec, HealthCheckSpec, LabelAndAnnotationSpec
from fiaas_deploy_daemon.tools import merge_dicts

SECRET_IMAGE = "fiaas/secret_image:version"
DATADOG_IMAGE = "fiaas/datadog_image:version"

INIT_CONTAINER_NAME = 'fiaas-secrets-init-container'
DATADOG_CONTAINER_NAME = 'fiaas-datadog-container'

SELECTOR = {'app': 'testapp'}
LABELS = {"deployment_deployer": "pass through", "global_label": "impossible to override"}
DEPLOYMENTS_URI = '/apis/extensions/v1beta1/namespaces/default/deployments/'


def test_make_http_probe():
    check_spec = CheckSpec(http=HttpCheckSpec(path="/", port=8080,
                                              http_headers={"Authorization": "ZmlubjpqdXN0aW5iaWViZXJfeG94bw=="}),
                           tcp=None, execute=None, initial_delay_seconds=30, period_seconds=60, success_threshold=3,
                           timeout_seconds=10)
    probe = _make_probe(check_spec)
    assert probe.httpGet.path == "/"
    assert probe.httpGet.port == 8080
    assert probe.httpGet.scheme == "HTTP"
    assert len(probe.httpGet.httpHeaders) == 1
    assert probe.httpGet.httpHeaders[0].name == "Authorization"
    assert probe.httpGet.httpHeaders[0].value == "ZmlubjpqdXN0aW5iaWViZXJfeG94bw=="
    assert probe.initialDelaySeconds == 30
    assert probe.periodSeconds == 60
    assert probe.successThreshold == 3
    assert probe.timeoutSeconds == 10


def test_make_tcp_probe():
    check_spec = CheckSpec(tcp=TcpCheckSpec(port=31337), http=None, execute=None, initial_delay_seconds=30,
                           period_seconds=60, success_threshold=3, timeout_seconds=10)
    probe = _make_probe(check_spec)
    assert probe.tcpSocket.port == 31337
    assert probe.initialDelaySeconds == 30
    assert probe.periodSeconds == 60
    assert probe.successThreshold == 3
    assert probe.timeoutSeconds == 10


def test_make_probe_should_fail_when_no_healthcheck_is_defined():
    check_spec = CheckSpec(tcp=None, execute=None, http=None, initial_delay_seconds=30, period_seconds=60,
                           success_threshold=3, timeout_seconds=10)
    with pytest.raises(RuntimeError):
        _make_probe(check_spec)


class TestDeploymentDeployer(object):
    @pytest.fixture(params=(True, False))
    def secret_init_container(self, request):
        yield request.param

    @pytest.fixture(params=(
        ("gke", {}),
        ("diy", {'A_GLOBAL_DIGIT': '0.01', 'A_GLOBAL_STRING': 'test'}),
        ("gke", {'A_GLOBAL_DIGIT': '0.01', 'A_GLOBAL_STRING': 'test', 'INFRASTRUCTURE': 'illegal', 'ARTIFACT_NAME': 'illegal'}),
    ))
    def config(self, request, secret_init_container):
        infra, global_env = request.param
        config = mock.create_autospec(Configuration([]), spec_set=True)
        config.infrastructure = infra
        config.environment = "test"
        config.global_env = global_env
        config.secrets_init_container_image = SECRET_IMAGE if secret_init_container else None
        config.secrets_service_account_name = "secretsmanager" if secret_init_container else None
        config.datadog_container_image = DATADOG_IMAGE
        yield config

    @pytest.fixture(params=(
        (True, 8080, {"foo": "bar", "global_label": "attempt to override"}, {"bar": "baz"}),
        (True, "8080", {"foo": "bar"}, {"bar": "baz"}),
        (True, "http", {"foo": "bar"}, {}),
        (False, None, {}, {}),
    ))
    def app_spec(self, request, app_spec):
        generic_toggle, prometheus_port, labels, annotations = request.param
        labels = LabelAndAnnotationSpec(deployment=labels, horizontal_pod_autoscaler={}, ingress={}, service={})
        annotations = LabelAndAnnotationSpec(deployment=annotations, horizontal_pod_autoscaler={}, ingress={},
                                             service={})
        if generic_toggle:
            ports = app_spec.ports
            health_checks = app_spec.health_checks
        else:
            ports = []
            exec_check = CheckSpec(http=None, tcp=None, execute=ExecCheckSpec(command="/app/check.sh"),
                                   initial_delay_seconds=10, period_seconds=10, success_threshold=1,
                                   timeout_seconds=1)
            health_checks = HealthCheckSpec(liveness=exec_check, readiness=exec_check)

        yield app_spec._replace(
            admin_access=generic_toggle,
            datadog=generic_toggle,
            ports=ports,
            health_checks=health_checks,
            secrets_in_environment=generic_toggle,
            prometheus=PrometheusSpec(generic_toggle, prometheus_port, '/internal-backstage/prometheus'),
            labels=labels,
            annotations=annotations
        )

    @pytest.mark.usefixtures("get")
    def test_deploy_new_deployment(self, request, post, config, app_spec):

        expected_deployment = create_expected_deployment(config, app_spec)

        deployer = DeploymentDeployer(config)
        deployer.deploy(app_spec, SELECTOR, LABELS)

        pytest.helpers.assert_any_call(post, DEPLOYMENTS_URI, expected_deployment)

    @pytest.mark.parametrize("previous_replicas,max_replicas,min_replicas,cpu_request,expected_replicas", (
            (5, 3, 2, None, 3),
            (5, 3, 2, "1", 5),
    ))
    def test_replicas_when_autoscaler_enabled(self, previous_replicas, max_replicas, min_replicas, cpu_request,
                                              expected_replicas, config, app_spec, get, put, post):
        deployer = DeploymentDeployer(config)

        image = "finntech/testimage:version2"
        version = "version2"
        app_spec = app_spec._replace(
            replicas=max_replicas,
            autoscaler=AutoscalerSpec(enabled=True, min_replicas=min_replicas, cpu_threshold_percentage=50),
            image=image)
        if cpu_request:
            app_spec = app_spec._replace(
                resources=ResourcesSpec(
                    requests=ResourceRequirementSpec(cpu=cpu_request, memory=None),
                    limits=ResourceRequirementSpec(cpu=None, memory=None)))

        uses_secrets_init_container = bool(config.secrets_init_container_image)
        expected_volumes = _get_expected_volumes(app_spec, uses_secrets_init_container)
        _, expected_volume_mounts = _get_expected_volume_mounts(app_spec, uses_secrets_init_container)

        mock_response = create_autospec(Response)
        mock_response.json.return_value = {
            'metadata': pytest.helpers.create_metadata('testapp', labels=LABELS),
            'spec': {
                'selector': {'matchLabels': SELECTOR},
                'template': {
                    'spec': {
                        'dnsPolicy': 'ClusterFirst',
                        'automountServiceAccountToken': False,
                        'serviceAccountName': 'default',
                        'restartPolicy': 'Always',
                        'volumes': expected_volumes,
                        'imagePullSecrets': [],
                        'containers': [{
                            'livenessProbe': {
                                'initialDelaySeconds': 10,
                                'periodSeconds': 10,
                                'successThreshold': 1,
                                'timeoutSeconds': 1,
                                'tcpSocket': {
                                    'port': 8080
                                }
                            },
                            'name': 'testapp',
                            'image': 'finntech/testimage:version',
                            'volumeMounts': expected_volume_mounts,
                            'env': create_environment_variables(config.infrastructure, global_env=config.global_env),
                            'envFrom': [{
                                'configMapRef': {
                                    'name': app_spec.name,
                                    'optional': True,
                                }
                            }],
                            'imagePullPolicy': 'IfNotPresent',
                            'readinessProbe': {
                                'initialDelaySeconds': 10,
                                'periodSeconds': 10,
                                'successThreshold': 1,
                                'timeoutSeconds': 1,
                                'httpGet': {
                                    'path': '/',
                                    'scheme': 'HTTP',
                                    'port': 8080,
                                    'httpHeaders': []
                                }
                            },
                            'ports': [{'protocol': 'TCP', 'containerPort': 8080, 'name': 'http'}],
                            'resources': {}
                        }]
                    },
                    'metadata': pytest.helpers.create_metadata('testapp', labels=LABELS)
                },
                'replicas': previous_replicas,
                'revisionHistoryLimit': 5
            }
        }
        get.side_effect = None
        get.return_value = mock_response

        deployer.deploy(app_spec, SELECTOR, LABELS)

        expected_deployment = create_expected_deployment(config, app_spec, image=image, version=version,
                                                         replicas=expected_replicas)
        pytest.helpers.assert_no_calls(post)
        pytest.helpers.assert_any_call(put, DEPLOYMENTS_URI + "testapp", expected_deployment)


def create_expected_deployment(config, app_spec, image='finntech/testimage:version', version="version", replicas=None):
    uses_secrets_init_container = bool(config.secrets_init_container_image)
    expected_volumes = _get_expected_volumes(app_spec, uses_secrets_init_container)
    expected_init_volume_mounts, expected_volume_mounts = _get_expected_volume_mounts(app_spec,
                                                                                      uses_secrets_init_container)

    if uses_secrets_init_container:
        init_containers = [{
            'name': INIT_CONTAINER_NAME,
            'image': SECRET_IMAGE,
            'volumeMounts': expected_init_volume_mounts,
            'env': [{'name': 'K8S_DEPLOYMENT', 'value': app_spec.name}],
            'envFrom': [{
                'configMapRef': {
                    'name': INIT_CONTAINER_NAME,
                    'optional': True,
                }
            }],
            'imagePullPolicy': 'IfNotPresent',
            'ports': []
        }]
        service_account_name = "secretsmanager"
    else:
        init_containers = []
        service_account_name = "default"

    base_expected_health_check = {
        'initialDelaySeconds': 10,
        'periodSeconds': 10,
        'successThreshold': 1,
        'timeoutSeconds': 1,
    }
    if app_spec.ports:
        expected_liveness_check = merge_dicts(base_expected_health_check, {
            'tcpSocket': {
                'port': 8080
            }
        })
        expected_readiness_check = merge_dicts(base_expected_health_check, {
            'httpGet': {
                'path': '/',
                'scheme': 'HTTP',
                'port': 8080,
                'httpHeaders': []
            }
        })
    else:
        exec_check = merge_dicts(base_expected_health_check, {
            'exec': {
                'command': ['/app/check.sh'],
            }
        })
        expected_liveness_check = exec_check
        expected_readiness_check = exec_check

    expected_env_from = [{
        'configMapRef': {
            'name': app_spec.name,
            'optional': True,
        }
    }]
    if app_spec.secrets_in_environment:
        expected_env_from.append({
            'secretRef': {
                'name': app_spec.name,
                'optional': True,
            }
        })

    resources = defaultdict(dict)
    if app_spec.resources.limits.cpu:
        resources["limits"]["cpu"] = app_spec.resources.limits.cpu
    if app_spec.resources.limits.memory:
        resources["limits"]["memory"] = app_spec.resources.limits.memory
    if app_spec.resources.requests.cpu:
        resources["requests"]["cpu"] = app_spec.resources.requests.cpu
    if app_spec.resources.requests.memory:
        resources["requests"]["memory"] = app_spec.resources.requests.memory
    resources = dict(resources)

    containers = [{
        'livenessProbe': expected_liveness_check,
        'name': app_spec.name,
        'image': image,
        'volumeMounts': expected_volume_mounts,
        'env': create_environment_variables(config.infrastructure, global_env=config.global_env,
                                            datadog=app_spec.datadog, version=version),
        'envFrom': expected_env_from,
        'imagePullPolicy': 'IfNotPresent',
        'readinessProbe': expected_readiness_check,
        'ports': [{'protocol': 'TCP', 'containerPort': 8080, 'name': 'http'}] if app_spec.ports else [],
        'resources': resources,
    }]
    if app_spec.datadog:
        containers.append({
            'name': DATADOG_CONTAINER_NAME,
            'image': DATADOG_IMAGE,
            'volumeMounts': [],
            'env': [
                {'name': 'DD_TAGS', 'value': "app:{},k8s_namespace:{}".format(app_spec.name, app_spec.namespace)},
                {'name': 'API_KEY', 'valueFrom': {'secretKeyRef': {'name': 'datadog', 'key': 'apikey'}}},
                {'name': 'NON_LOCAL_TRAFFIC', 'value': 'false'},
                {'name': 'DD_LOGS_STDOUT', 'value': 'yes'}
            ],
            'envFrom': [],
            'imagePullPolicy': 'IfNotPresent',
            'ports': [],
        })
    deployment_annotations = app_spec.annotations.deployment if app_spec.annotations.deployment else None
    deployment = {
        'metadata': pytest.helpers.create_metadata(app_spec.name,
                                                   labels=merge_dicts(app_spec.labels.deployment, LABELS),
                                                   annotations=deployment_annotations),
        'spec': {
            'selector': {'matchLabels': SELECTOR},
            'template': {
                'spec': {
                    'dnsPolicy': 'ClusterFirst',
                    'automountServiceAccountToken': uses_secrets_init_container or app_spec.admin_access,
                    'serviceAccountName': service_account_name,
                    'restartPolicy': 'Always',
                    'volumes': expected_volumes,
                    'imagePullSecrets': [],
                    'containers': containers,
                    'initContainers': init_containers
                },
                'metadata': pytest.helpers.create_metadata(app_spec.name, prometheus=app_spec.prometheus.enabled,
                                                           labels=_get_expected_template_labels())
            },
            'replicas': replicas if replicas else app_spec.replicas,
            'revisionHistoryLimit': 5
        }
    }
    return deployment


def create_environment_variables(infrastructure, global_env=None, version="version", datadog=False):
    environment = [{'name': 'ARTIFACT_NAME', 'value': 'testapp'},
                   {'name': 'LOG_STDOUT', 'value': 'true'},
                   {'name': 'VERSION', 'value': version},
                   {'name': 'CONSTRETTO_TAGS', 'value': 'kubernetes-test,kubernetes,test'},
                   {'name': 'FIAAS_INFRASTRUCTURE', 'value': infrastructure},
                   {'name': 'FIAAS_ENVIRONMENT', 'value': 'test'},
                   {'name': 'LOG_FORMAT', 'value': 'json'},
                   {'name': 'IMAGE', 'value': 'finntech/testimage:' + version},
                   {'name': 'FINN_ENV', 'value': 'test'}, ]
    if global_env:
        environment.append({'name': 'A_GLOBAL_STRING', 'value': global_env['A_GLOBAL_STRING']})
        environment.append({'name': 'FIAAS_A_GLOBAL_STRING', 'value': global_env['A_GLOBAL_STRING']})
        environment.append({'name': 'A_GLOBAL_DIGIT', 'value': global_env['A_GLOBAL_DIGIT']})
        environment.append({'name': 'FIAAS_A_GLOBAL_DIGIT', 'value': global_env['A_GLOBAL_DIGIT']})
    if datadog:
        environment.append({'name': 'STATSD_HOST', 'value': 'localhost'})
        environment.append({'name': 'STATSD_PORT', 'value': '8125'})
    return environment


def _get_expected_template_labels():
    expected_template_labels = {"fiaas/status": "active"}
    expected_template_labels.update(LABELS)
    return expected_template_labels


def _get_expected_volumes(app_spec, uses_secrets_init_container):
    secret_volume = {
        'name': "{}-secret".format(app_spec.name),
        'secret': {
            'secretName': app_spec.name,
            'optional': True
        }
    }
    init_secret_volume = {
        'name': "{}-secret".format(app_spec.name),
    }
    config_map_volume = {
        'name': "{}-config".format(app_spec.name),
        'configMap': {
            'name': app_spec.name,
            'optional': True
        }
    }
    init_config_map_volume = {
        'name': "{}-config".format(INIT_CONTAINER_NAME),
        'configMap': {
            'name': INIT_CONTAINER_NAME,
            'optional': True
        }
    }
    tmp_volume = {
        'name': "tmp"
    }
    if uses_secrets_init_container:
        expected_volumes = [init_secret_volume, init_config_map_volume, config_map_volume, tmp_volume]
    else:
        expected_volumes = [secret_volume, config_map_volume, tmp_volume]
    return expected_volumes


def _get_expected_volume_mounts(app_spec, uses_secrets_init_container):
    secret_volume_mount = {
        'name': "{}-secret".format(app_spec.name),
        'readOnly': True,
        'mountPath': '/var/run/secrets/fiaas/'
    }
    init_secret_volume_mount = {
        'name': "{}-secret".format(app_spec.name),
        'readOnly': False,
        'mountPath': '/var/run/secrets/fiaas/'
    }
    config_map_volume_mount = {
        'name': "{}-config".format(app_spec.name),
        'readOnly': True,
        'mountPath': '/var/run/config/fiaas/'
    }
    init_config_map_volume_mount = {
        'name': "{}-config".format(INIT_CONTAINER_NAME),
        'readOnly': True,
        'mountPath': "/var/run/config/{}/".format(INIT_CONTAINER_NAME)
    }
    tmp_volume_mount = {
        'name': "tmp",
        'readOnly': False,
        'mountPath': "/tmp"
    }
    expected_volume_mounts = [secret_volume_mount, config_map_volume_mount, tmp_volume_mount]
    if uses_secrets_init_container:
        expected_init_volume_mounts = [init_secret_volume_mount, init_config_map_volume_mount, config_map_volume_mount, tmp_volume_mount]
    else:
        expected_init_volume_mounts = []
    return expected_init_volume_mounts, expected_volume_mounts

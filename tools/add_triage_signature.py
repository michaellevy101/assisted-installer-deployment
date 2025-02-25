#!/usr/bin/env python3
# pylint: disable=invalid-name,broad-except,missing-function-docstring,too-few-public-methods,missing-class-docstring
# pylint: disable=missing-module-docstring,line-too-long,too-many-locals,logging-fstring-interpolation
import abc
import argparse
import functools
import json
import logging
import os
import re
import itertools
import sys
import tempfile
from collections import OrderedDict, defaultdict
from datetime import datetime
from textwrap import dedent
from enum import Flag, auto

import colorlog
import dateutil.parser
import jira
import nestedarchive
import requests
import tqdm
from fuzzywuzzy import fuzz
from tabulate import tabulate

import consts

DEFAULT_DAYS_TO_HANDLE = 30
CF_USER = "12319044"
CF_DOMAIN = "12319045"
CF_CLUSTER_ID = "12316349"
CF_FUNCTION_IMPACT = "12317358"
CF_IGNORED_DOMAINS = {"redhat.com", "juniper.net"}


logger = logging.getLogger(__name__)

JIRA_DESCRIPTION = """
{{color:red}}Do not manually edit this description, it will get automatically over-written{{color}}
h1. Cluster Info

*Cluster ID:* [{cluster_id}|https://issues.redhat.com/issues/?jql=project%20%3D%20AITRIAGE%20AND%20component%20%3D%20Cloud-Triage%20and%20cf%5B{cf_cluster_id}%5D%20~%20"{cluster_id}"]
*Username:* [{username}|https://issues.redhat.com/issues/?jql=project%20%3D%20AITRIAGE%20AND%20component%20%3D%20Cloud-Triage%20and%20cf%5B{cf_user}%5D%20~%20"{username}"]
*Email domain:* [{domain}|https://issues.redhat.com/issues/?jql=project%20%3D%20AITRIAGE%20AND%20component%20%3D%20Cloud-Triage%20and%20cf%5B{cf_domain}%5D%20~%20"{domain}"]
*Created_at:* {created_at}
*Installation started at:* {installation_started_at}
*Failed on:* {failed_on}
*status:* {status}
*status_info:* {status_info}
*OpenShift version:* {openshift_version}
*Olm Operators:* {operators}
*Configured features:* {features}

*links:*
* [Cluster on prod|https://cloud.redhat.com/openshift/assisted-installer/clusters/{cluster_id}]
* [Logs|{logs_url}]
* [Kraken|https://kraken.psi.redhat.com/clusters/{OCP_cluster_id}]
* [Metrics|https://grafana.app-sre.devshift.net/d/assisted-installer-cluster-overview/cluster-overview?orgId=1&from=now-1h&to=now&var-datasource=app-sre-prod-04-prometheus&var-clusterId={cluster_id}]
* [Kibana|https://kibana-openshift-logging.apps.app-sre-prod-04.i5h0.p1.openshiftapps.com/app/kibana#/discover?_g=(refreshInterval:(pause:!t,value:0),time:(from:now-24h,mode:quick,to:now))&_a=(columns:!(_source),interval:auto,query:'"{cluster_id}"',sort:!('@timestamp',desc))]
* [DM Elastic|http://assisted-elastic.usersys.redhat.com:5601/app/discover#/?_g=(filters:!(),refreshInterval:(pause:!t,value:0),time:(from:now-2w,to:now))&_a=(columns:!(_source),filters:!(),index:bd9dadc0-7bfa-11eb-95b8-d13a1970ae4d,interval:auto,query:(language:lucene,query:'cluster.id:%20%22{cluster_id}%22'),sort:!())]
* [DM Elastic Dashboard|http://assisted-elastic.usersys.redhat.com:5601/app/dashboards#/view/500e1d40-58d6-11eb-8ff7-115676c7222d?_g=(filters:!(),query:(language:kuery,query:'cluster_id:%20%22{cluster_id}%22'),refreshInterval:(pause:!t,value:0),time:(from:now-2w,to:now))&_a=(description:'',filters:!(),fullScreenMode:!f,options:(hidePanelTitles:!f,useMargins:!t),query:(language:kuery,query:'cluster_id:%20%22{cluster_id}%22%20'),timeRestore:!f,title:'cluster%20overview',viewMode:view)]
"""  # noqa


def custom_field_name(custom_field):
    return f"customfield_{custom_field}"


def config_logger(verbose):
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter('%(log_color)s%(levelname)-10s %(message)s'))

    logger = logging.getLogger("__main__")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    if verbose:
        logger.setLevel(logging.DEBUG)


def format_description(failure_data):
    return JIRA_DESCRIPTION.format(**failure_data)


def days_ago(datestr):
    try:
        return (datetime.now() - dateutil.parser.isoparse(datestr)).days
    except Exception:
        logger.debug("Cannot parse date: %s", datestr)
        return 9999


class FailedToGetMetadataException(Exception):
    pass


@functools.lru_cache(maxsize=1000)
def get_metadata_json(cluster_url):
    try:
        res = requests.get("{}/metadata.json".format(cluster_url))
        res.raise_for_status()
        return res.json()
    except Exception as e:
        raise FailedToGetMetadataException from e


@functools.lru_cache(maxsize=1000)
def get_events_json(cluster_url, cluster_id):
    res = requests.get(f"{cluster_url}/cluster_{cluster_id}_events.json")
    res.raise_for_status()
    return res.json()


@functools.lru_cache(maxsize=100)
def get_remote_archive(tar_url):
    return nestedarchive.RemoteNestedArchive(tar_url, init_download=True)


class FailedToGetLogsTarException(Exception):
    pass


def get_triage_logs_tar(triage_url, cluster_id):
    try:
        tar_url = f"{triage_url}/cluster_{cluster_id}_logs.tar"
        return get_remote_archive(tar_url)
    except requests.exceptions.HTTPError as e:
        raise FailedToGetLogsTarException from e


def get_host_log_file(triage_logs_tar, host_id, filename):
    # The file is already uniquely determined by the host_id, we can omit the hostname
    hostname = "*"

    return triage_logs_tar.get(f"{hostname}.tar.gz/logs_host_{host_id}/{filename}")


def get_journal(triage_logs_tar, host_ip, journal_file):
    return triage_logs_tar.get(
        f"*_bootstrap_*.tar.gz/logs_host_*/log-bundle-*.tar.gz/log-bundle-*/control-plane/{host_ip}/journals/{journal_file}"
    )


############################
# Common functionality
############################
class Signature(abc.ABC):
    dry_run_file = None

    def __init__(self, jira_client, comment_identifying_string, old_comment_string=None):
        self._jclient = jira_client
        self._identifing_string = comment_identifying_string
        self._old_identifing_string = old_comment_string

    def update_ticket(self, url, issue_key, should_update=False):
        try:
            self._update_ticket(url, issue_key, should_update=should_update)
        except FailedToGetMetadataException as e:
            browse_url = get_ticket_browse_url(issue_key)
            logger.error(
                f"Error getting metadata for {browse_url} at {url}: {e.__cause__}, it may have been deleted"
            )
        except FailedToGetLogsTarException as e:
            if e.__cause__.response.status_code == 404:
                logger.warning(
                    f"{get_ticket_browse_url(issue_key)} doesn't have a log tar, skipping {type(self).__name__}")
                return
            raise
        except Exception:
            logger.exception("error updating ticket %s", issue_key)

    @abc.abstractmethod
    def _update_ticket(self, url, issue_key, should_update=False):
        pass

    def _add_signature_label(self, key, labels):
        issue_labels = self._jclient.issue(key).fields.__dict__[custom_field_name(CF_FUNCTION_IMPACT)]
        if issue_labels is None:
            issue_labels = []
        is_changed = False
        for label in labels:
            if label not in issue_labels:
                is_changed = True
                issue_labels.append(label)
        if is_changed:
            self._update_fields(key, {custom_field_name(CF_FUNCTION_IMPACT): issue_labels})

    def find_signature_comment(self, key=None, comments=None):
        assert key or comments

        if comments is None:
            comments = self._jclient.comments(key)

        for comment in comments:
            if self._identifing_string in comment.body:
                return comment

            if self._old_identifing_string and self._old_identifing_string in comment.body:
                return comment
        return None

    def _update_triaging_ticket(self, key, comment, should_update=False):
        report = "\n"
        report += self._identifing_string + "\n"
        if comment is not None:
            report += comment

        jira_comment = self.find_signature_comment(key)
        signature_name = type(self).__name__
        if self.dry_run_file is not None:
            self.dry_run_file.write(report)
            return

        if jira_comment is None:
            logger.info("Adding new '%s' comment to %s", signature_name, key)
            self._jclient.add_comment(key, report)
        elif should_update:
            logger.info("Updating existing '%s' comment of %s", signature_name, key)
            jira_comment.update(body=report)
        else:
            logger.debug("Not updating existing '%s' comment of %s", signature_name, key)

    def _update_description(self, key, new_description):
        i = self._jclient.issue(key)
        i.update(fields={"description": new_description})

    def _update_fields(self, key, fields_dict):
        i = self._jclient.issue(key)
        i.update(fields=fields_dict)

    @staticmethod
    def _generate_table_for_report(hosts):
        return tabulate(hosts, headers="keys", tablefmt="jira") + "\n"

    @staticmethod
    def _logs_url_to_api(url):
        '''
        the log server has two formats for the url
        - URL for the UI  - http://assisted-logs-collector.usersys.redhat.com/#/2020-10-15_19:10:06_347ce6e8-bb4d-4751-825f-5e92e24da0d9/
        - URL for the API - http://assisted-logs-collector.usersys.redhat.com/files/2020-10-15_19:10:06_347ce6e8-bb4d-4751-825f-5e92e24da0d9/
        This function will return an API URL, regardless of which URL is supplied
        '''
        return re.sub(r'(http://[^/]*/)#(/.*)', r'\1files\2', url)

    @staticmethod
    def _logs_url_to_ui(url):
        '''
        the log server has two formats for the url
        - URL for the UI  - http://assisted-logs-collector.usersys.redhat.com/#/2020-10-15_19:10:06_347ce6e8-bb4d-4751-825f-5e92e24da0d9/
        - URL for the API - http://assisted-logs-collector.usersys.redhat.com/files/2020-10-15_19:10:06_347ce6e8-bb4d-4751-825f-5e92e24da0d9/
        This function will return an UI URL, regardless of which URL is supplied
        '''
        return re.sub(r'(http://[^/]*/)files(/.*)', r'\1#\2', url)

    @staticmethod
    def _get_hostname(host):
        hostname = host.get('requested_hostname')
        if hostname:
            return hostname

        inventory = json.loads(host['inventory'])
        return inventory['hostname']


class ErrorSignature(Signature):
    def __init__(self, jira_client, signature_label, comment_identifying_string,
                 old_comment_string=None):
        Signature.__init__(self, jira_client, comment_identifying_string,
                           old_comment_string=old_comment_string)
        self._signature_label = signature_label

    def _update_triaging_ticket(self, key, comment, should_update=False):
        Signature._update_triaging_ticket(self, key, comment, should_update=should_update)
        if should_update:
            self._add_signature_label(key, ["SIGNATURE_"+self._signature_label])


class HostsStatusSignature(Signature):
    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="h1. Install status:",
                         old_comment_string="h1. Host details:")

    def _update_ticket(self, url, issue_key, should_update=False):

        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        cluster = md['cluster']

        hosts = []
        for host in cluster['hosts']:
            info = host['status_info']
            role = host['role']
            if host.get('bootstrap', False):
                role = 'bootstrap'
            hosts.append(OrderedDict(
                id=host['id'],
                hostname=self._get_hostname(host),
                progress=host['progress']['current_stage'],
                status=host['status'],
                role=role,
                status_info=str(info),
                logs_info=host.get('logs_info', ""),
                last_checked_in_at=format_time(host.get('checked_in_at', str(datetime.min)))))

        report = "h2. Cluster Status\n"
        report += "*status:* {}\n".format(cluster['status'])
        report += "*status_info:* {}\n".format(cluster['status_info'])
        report += "h2. Hosts status\n"
        report += self._generate_table_for_report(hosts)
        self._update_triaging_ticket(issue_key, report, should_update=should_update)


class FailureDescription(Signature):
    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="")

    def is_olm_operator(self, operator_name):
        return operator_name.lower() in ["cnv", "lso", "ocs"]

    def format_features(self, features):
        if len(features) > 0:
            return ', '.join(features)
        else:
            return "None"

    def build_feature_description(self, cluster):
        """
        feature_usage field on the cluster holds a JSON structure.
        It's keys are features names where feature is any capacity
        configured by the user (including Olm operators).
        For sake of display, operators and other features are listed
        separately
        """
        feature_usage_field = cluster.get('feature_usage')
        if not feature_usage_field:
            return "n/a", "n/a"

        feature_usage = json.loads(feature_usage_field)
        operators = [feature for feature in feature_usage.keys() if self.is_olm_operator(feature)]
        other_features = [feature for feature in feature_usage.keys() if not self.is_olm_operator(feature)]
        return self.format_features(operators), self.format_features(other_features)

    def build_description(self, url, cluster_md):
        operators, other_features = self.build_feature_description(cluster_md)
        cluster_data = {"cluster_id": cluster_md['id'],
                        "logs_url": self._logs_url_to_ui(url).strip('/') + '/',
                        "domain": cluster_md['email_domain'],
                        "openshift_version": cluster_md['openshift_version'],
                        "created_at": format_time(cluster_md['created_at']),
                        "installation_started_at": format_time(cluster_md['install_started_at']),
                        "failed_on": format_time(cluster_md['status_updated_at']),
                        "status": cluster_md['status'],
                        "status_info": cluster_md['status_info'],
                        "OCP_cluster_id": cluster_md['openshift_cluster_id'],
                        "username": cluster_md['user_name'],
                        "operators": operators,
                        "features": other_features,
                        "cf_user": CF_USER,
                        "cf_cluster_id": CF_CLUSTER_ID,
                        "cf_domain": CF_DOMAIN}

        return format_description(cluster_data)

    def _update_ticket(self, url, issue_key, should_update=False):

        if not should_update:
            logger.debug("Not updating description of %s", issue_key)
            return

        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        cluster = md['cluster']

        description = self.build_description(url, cluster)

        logger.info("Updating description of %s", issue_key)
        self._update_description(issue_key, description)


class FailureDetails(Signature):
    """
    This signature sets some fields such as:
        cluster-id
        user
        domain
    """

    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="")

    def is_olm_operator(self, operator_name):
        return operator_name.lower() in ["cnv", "lso", "ocs"]

    def _update_ticket(self, url, issue_key, should_update=False):

        def extract_cluster_md_from_existing_labels():
            label_prefix_to_property = {
                "AI_CLUSTER_": "id",
                "AI_DOMAIN_": "email_domain",
                "AI_USER_": "user_name",
            }
            r = re.compile(r'(AI_[^_]*_)(.*)')

            i = self._jclient.issue(issue_key)
            cluster_md = {}
            for label in i.fields.labels:
                m = r.match(label)
                if m is None or len(m.groups()) != 2:
                    continue
                prefix, value = m.groups()
                if prefix not in label_prefix_to_property:
                    continue
                cluster_md[label_prefix_to_property[prefix]] = value
            return cluster_md

        url = self._logs_url_to_api(url)
        cluster_md = []
        try:
            md = get_metadata_json(url)
            cluster_md = md['cluster']
        except Exception:
            # if we cannot find the failure logs on the log-server, we'll just use whatever information we
            # have in the 'labels' field of the ticket
            # this is mainly relevant for triage tickets that were moved from 'MGMT' project to 'AITRIAGE'
            # project
            cluster_md = extract_cluster_md_from_existing_labels()

        update_fields = {
            "labels": [],
        }
        if 'user_name' in cluster_md:
            update_fields[custom_field_name(CF_USER)] = cluster_md['user_name']
        if 'email_domain' in cluster_md:
            update_fields[custom_field_name(CF_DOMAIN)] = cluster_md['email_domain']
        if 'id' in cluster_md:
            update_fields[custom_field_name(CF_CLUSTER_ID)] = cluster_md['id']

        feature_usage_field = cluster_md.get('feature_usage')
        if feature_usage_field:
            feature_usage = json.loads(feature_usage_field)
            operators = [feature for feature in feature_usage.keys() if self.is_olm_operator(feature)]
            other_features = [feature for feature in feature_usage.keys() if not self.is_olm_operator(feature)]
            if len(operators+other_features) > 0:
                labels = []
                for f in operators+other_features:
                    labels.append("FEATURE-"+f.replace(" ", "-"))

                update_fields["labels"] = labels

        logger.info("Updating fields of %s", issue_key)
        self._update_fields(issue_key, update_fields)


class HostsExtraDetailSignature(Signature):
    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="h1. Host extra details:")

    def _update_ticket(self, url, issue_key, should_update=False):

        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        cluster = md['cluster']

        hosts = []
        for host in cluster['hosts']:
            inventory = json.loads(host['inventory'])
            hosts.append(OrderedDict(
                id=host['id'],
                hostname=inventory['hostname'],
                requested_hostname=host.get('requested_hostname', "N/A"),
                last_contacted=format_time(host['checked_in_at']),
                installation_disk=host.get('installation_disk_path', "N/A"),
                product_name=inventory['system_vendor'].get('product_name', "Unavailable"),
                manufacturer=inventory['system_vendor'].get('manufacturer', "Unavailable"),
                virtual_host=inventory['system_vendor'].get('virtual', False),
                disks_count=len(inventory['disks'])
            ))

        report = self._generate_table_for_report(hosts)
        self._update_triaging_ticket(issue_key, report, should_update=should_update)


class InstallationDiskFIOSignature(Signature):
    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="h1. Host slow installation disks:")

    @staticmethod
    def _get_fio_events(events):
        fio_regex = re.compile(r'\(fdatasync duration:\s(\d+)\sms\)')

        def get_duration(event):
            matches = fio_regex.findall(event["message"])
            if len(matches) == 0:
                return None

            return int(matches[0])

        return (
            (event, get_duration(event))
            for event in events
            if get_duration(event) is not None
        )

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        cluster = md['cluster']

        cluster_id = cluster['id']
        events = get_events_json(cluster_url=url, cluster_id=cluster_id)
        fio_events = self._get_fio_events(events)

        fio_events_by_host = defaultdict(list)
        for event, fio_duration in fio_events:
            fio_events_by_host[event["host_id"]].append((event, fio_duration))

        hosts = []
        for host in cluster['hosts']:
            host_fio_events = fio_events_by_host[host["id"]]
            if len(host_fio_events) != 0:
                _events, host_fio_events_durations = zip(*fio_events_by_host[host["id"]])
                fio_message = (
                    "{color:red}Installation disk is too slow, fio durations: " +
                    ", ".join(f"{duration}ms" for duration in host_fio_events_durations) +
                    "{color}"
                )

                hosts.append(OrderedDict(
                    id=host['id'],
                    hostname=self._get_hostname(host),
                    fio=fio_message,
                    installation_disk=host.get('installation_disk_path', ""),
                ))

        if len(hosts) > 0:
            report = self._generate_table_for_report(hosts)
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class CNIConfigurationError(ErrorSignature):
    def __init__(self, jira_client):
        super().__init__(jira_client, signature_label="missing_CNI",
                         comment_identifying_string="h1. No CNI configuration")

    @staticmethod
    def _get_host_neighbors(host):
        """
        Given a host object, get the IP addresses of neighboring hosts. These are extracted from the given host's "connectivity" stanza.
        """
        if host["connectivity"] is None:
            return []
        try:
            return [(remote_host['host_id'], remote_host['l3_connectivity'][0]['remote_ip_address'])
                    for remote_host in
                    json.loads(host["connectivity"])['remote_hosts']]
        except KeyError:
            # Malformed host obect, ignore
            return []

    @classmethod
    def _get_all_host_ip_addresses(cls, cluster):
        """Given a cluster object, return a list of all host IP addresses.

        Since the metadata.json file does not contain the IP address of each host,
        we have to infer it by examining the connectivity stanza of each of the hosts.
        Collecting the neighboring hosts from each of the host objects into a set should hopefully
        give us all host IP addresses.
        """
        return set(itertools.chain(*(cls._get_host_neighbors(host) for host in cluster['hosts'])))

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)

        md = get_metadata_json(url)

        cluster = md['cluster']
        cluster_id = cluster['id']

        triage_logs_tar = get_triage_logs_tar(triage_url=url, cluster_id=cluster_id)

        host_ip_addresses = self._get_all_host_ip_addresses(cluster)

        if len(host_ip_addresses) == 0:
            # Fallback because some clusters just have a weird metadata.json without even a `hosts` stanza...
            host_ip_addresses = [('Unknown', '*')]

        hosts = []
        threshold = 1000

        for host_id, host_ip in host_ip_addresses:
            try:
                kubelet_journal = get_journal(triage_logs_tar, host_ip, "kubelet.log")
                cni_errors = search_patterns_in_string(kubelet_journal, re.escape(
                    "No CNI configuration file in /etc/kubernetes/cni/net.d/. Has your network provider started?"))

                if len(cni_errors) > threshold:
                    hosts.append(OrderedDict({
                        "Host ID": host_id,
                        "Host IP": host_ip,
                        "Number of errors": f"{len(cni_errors)} (threshold {threshold})",
                        "First error": f"{{code}}{cni_errors[0]}{{code}}",
                        "Last error": f"{{code}}{cni_errors[1]}{{code}}",
                    }))
            except FileNotFoundError:
                continue

        if len(hosts):
            report = self._generate_table_for_report(hosts)
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class StorageDetailSignature(Signature):
    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="h1. Host storage details:")

    class SmartctlExitCode(Flag):
        """Taken from `man smartctl` "EXIT STATUS" section"""
        COMMANDLINE_DID_NOT_PARSE = auto()
        DEVICE_OPEN_FAILED = auto()
        SOME_ATA_COMMAND_FAILED_OR_SMART_CHECKSUM_ERROR = auto()
        DISK_FAILING = auto()
        PREFAIL_ATTRIBUTRES_UNDER_THRESHOLD = auto()
        DISK_OK_SOME_ATTRIBUTES_UNDER_THRESHOLD = auto()
        LOG_HAS_RECORDS_OF_ERRORS = auto()
        SELF_TEST_LOG_HAS_RECORDS_OF_ERRORS = auto()

    @classmethod
    def _parse_smart_internal(cls, smart):
        output = []
        smartctl = smart["smartctl"]
        exit_code = smartctl["exit_status"]

        if exit_code != 0:
            output.append("non-zero smartctl exit code: " + ", ".join(flag.name for flag in cls.SmartctlExitCode
                                                                      if flag in cls.SmartctlExitCode(exit_code)))
            if "smart_status" not in smart:
                if cls.SmartctlExitCode(exit_code) == cls.SmartctlExitCode.SOME_ATA_COMMAND_FAILED_OR_SMART_CHECKSUM_ERROR:
                    output.append(
                        "{color:green}*this typically happens on virtual disks*{color} that don't actually have S.M.A.R.T. information,"
                        " but may also happen due to other reasons")
                if cls.SmartctlExitCode(exit_code) == cls.SmartctlExitCode.COMMANDLINE_DID_NOT_PARSE:
                    output.append("{color:green}*this is not a disk problem*{color} but the smartctl runtime issue")

        for message in smartctl.get("messages", []):
            severity = message.get("severity", "unknown")
            message_string = message.get("string", "")
            output.append(f'smartctl message with severity "{severity}": "{message_string}"')

        if "smart_status" in smart:
            passed = smart["smart_status"]["passed"]
            if passed:
                if "model_name" in smart:
                    if smart["model_name"] == "QEMU HARDDISK":
                        return "{color:green}*Virtual QEMU HARDDISK pretending to have S.M.A.R.T.*{color}"

                output.append("{color:green}*Disk S.M.A.R.T. status OK*{color}")
            else:
                output.append("{color:red}*Disk S.M.A.R.T. status BAD*{color}")

        if "power_cycle_count" in smart:
            output.append(f"disk has been power cycled {smart['power_cycle_count']} times throughout its life")

        if "power_on_time" in smart:
            if "hours" in smart["power_on_time"]:
                hours = smart["power_on_time"]["hours"]
                output.append(
                    f"disk has been powered on for {hours // 24} days and {hours % 24} hours throughout its life")

        if "ata_smart_attributes" in smart:
            table = smart["ata_smart_attributes"].get("table", [])
            interesting_attributes = ("Program_Fail_Count", "Erase_Fail_Count", "Offline_Uncorrectable")
            table_filtered = [entry for entry in table if
                              entry.get("name") in interesting_attributes and
                              entry.get("raw")["value"] > 0]

            output.extend(f'ata_smart_attribute *{entry["name"]}* has notable value *{entry["raw"]["value"]}*'
                          for entry in table_filtered)

        if "nvme_smart_health_information_log" in smart:
            nvme_log = smart["nvme_smart_health_information_log"]
            if "percentage_used" in nvme_log:
                output.append(f"SSD percentage used: %{nvme_log['percentage_used']}")

        return ", ".join(output)

    @classmethod
    def _parse_smart(cls, smart_json):
        try:
            return cls._parse_smart_internal(json.loads(smart_json))
        except json.JSONDecodeError as e:
            return f"Failed to decode S.M.A.R.T. JSON, error: {e}"
        except Exception as e:
            return f"S.M.A.R.T. JSON has an unexpected structure, error: {e}"

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        cluster = md['cluster']

        hosts = []
        for host in cluster['hosts']:
            inventory = json.loads(host['inventory'])
            disks = inventory['disks']
            disks_details = defaultdict(list)
            for d in disks:
                disk_type = d.get('drive_type', "N/A")
                disks_details['type'].append(disk_type)
                disks_details['bootable'].append(str(d.get('bootable', "N/A")))
                disks_details['name'].append(d.get('name', "N/A"))
                disks_details['path'].append(d.get('path', "N/A"))
                disks_details['by-path'].append(d.get('by_path', "N/A"))
                disks_details['smart'].append((self._parse_smart(d.get('smart', "N/A"))
                                               if disk_type != 'ODD' else
                                               "{color:green}*N/A for optical drives*{color}"))
            hosts.append(OrderedDict({
                "Host ID": host['id'],
                "Hostname": self._get_hostname(host),
                "Disk Name": "\n".join(disks_details['name']),
                "Disk Type": "\n".join(disks_details['type']),
                "Disk Path": "\n".join(disks_details['path']),
                "Disk Bootable": "\n".join(disks_details['bootable']),
                "Disk by-path": "\n".join(disks_details['by-path']),
                "S.M.A.R.T.": "\n".join(disks_details['smart']),
            }))

        report = self._generate_table_for_report(hosts)
        self._update_triaging_ticket(issue_key, report, should_update=should_update)


class ComponentsVersionSignature(Signature):
    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="h1. Components version information:")

    def _update_ticket(self, url, issue_key, should_update=False):

        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        report = ""
        release_tag = md.get('release_tag')
        if release_tag:
            report = "Release tag: {}\n".format(release_tag)

        versions = md.get('versions')
        if versions:
            if "assisted-installer" in versions:
                report += "assisted-installer: {}\n".format(versions['assisted-installer'])
            if "assisted-installer-controller" in versions:
                report += "assisted-installer-controller: {}\n".format(versions['assisted-installer-controller'])
            if "discovery-agent" in versions:
                report += "assisted-installer-agent: {}\n".format(versions['discovery-agent'])

        if report != "":
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class LibvirtRebootFlagSignature(ErrorSignature):
    def __init__(self, jira_client):
        super().__init__(jira_client, signature_label="libvirt_reboot_flag",
                         comment_identifying_string="h1. Potential hosts with libvirt _on_reboot_ flag issue (MGMT-2840):")

    def _update_ticket(self, url, issue_key, should_update=False):

        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)

        cluster = md['cluster']

        # this signature is not relevant for SNO
        if len(cluster['hosts']) <= 1:
            return

        # this signature is relevant only if all hosts, but the bootstrap is in 'Rebooting' stage
        hosts = []
        for host in cluster['hosts']:
            inventory = json.loads(host['inventory'])

            if (len(inventory['disks']) == 1 and "KVM" in inventory['system_vendor']['product_name'] and
                    host['progress']['current_stage'] == 'Rebooting' and host['status'] == 'error'):
                if host['role'] == 'bootstrap':
                    return

                hosts.append(OrderedDict(
                    id=host['id'],
                    hostname=self._get_hostname(host),
                    role=host['role'],
                    progress=host['progress']['current_stage'],
                    status=host['status'],
                    num_disks=len(inventory.get('disks', []))))

        if len(hosts)+1 == len(cluster['hosts']):
            report = self._generate_table_for_report(hosts)
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class ApiInvalidCertificateSignature(ErrorSignature):

    LOG_PATTERN = re.compile('time=".*" level=error msg=".*x509: certificate is valid.* not .*')

    def __init__(self, jira_client):
        super().__init__(jira_client, signature_label="api_invalid_certificate",
                         comment_identifying_string="h1. Invalid SAN values on certificate for AI API")

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)

        md = get_metadata_json(url)

        cluster = md['cluster']
        cluster_id = cluster['id']
        triage_logs_tar = get_triage_logs_tar(triage_url=url, cluster_id=cluster_id)

        try:
            controller_logs = triage_logs_tar.get("controller_logs.tar.gz/assisted-installer-controller-*.logs")
        except FileNotFoundError:
            return

        invalid_api_log_lines = self.LOG_PATTERN.findall(controller_logs)
        if invalid_api_log_lines:
            ticket_inform = "see: https://issues.redhat.com/browse/MGMT-4039\n"
            logs_text = "{code}" + "\n".join(invalid_api_log_lines[:5]) + "{code}"

            if len(invalid_api_log_lines) > 5:
                logs_text += "\n additional {} relevant similar error log lines are found".format(
                    len(invalid_api_log_lines) - 5)
                report = dedent("""{}""".format(ticket_inform + logs_text))
            else:
                report = None

            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class AllInstallationAttemptsSignature(Signature):
    def __init__(self, jira_client):
        self.jira_client = jira_client
        super().__init__(jira_client, comment_identifying_string="h1. all installation attempts of a cluster")

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)
        cluster = md['cluster']
        cluster_id = cluster['id']
        cluster_triage_tickets = self.jira_client.search_issues(
            f"""project = AITRIAGE AND "Cluster ID" ~ {cluster_id}""")
        for ticket in cluster_triage_tickets:
            if issue_key == ticket.key:
                continue

            self.jira_client.create_issue_link("is related to", issue_key, ticket.key)


class MediaDisconnectionSignature(ErrorSignature):
    '''
    The signature downloads cluster logs, and go over all the hosts logs files searching for a 'dmesg' file
    Then, it looks for i/o errors (disks, media) patterns and groups them per host
    Eventually, the signature will include common i/o errors and the number of times they appeared
    '''

    ERRORS_PATTERNS = 'media was likely disconnected'

    def __init__(self, jira_client):
        super().__init__(jira_client, signature_label="media_disconnect",
                         comment_identifying_string="h1. Virtual media disconnection")

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)
        md = get_metadata_json(url)
        cluster = md['cluster']

        hosts = list()
        for host in cluster['hosts']:
            status_info = host['status_info']
            if self.ERRORS_PATTERNS in status_info:
                hosts.append(
                    OrderedDict(
                        id=host["id"],
                        hostname=self._get_hostname(host),
                        status_info=f"{{color:red}}{status_info}{{color}}",
                    )
                )

        if len(hosts) != 0:
            report = dedent("""
            Media disconnection events were found.
            It usually indicates that reading data from media disk cannot be made due to a network problem on PXE setup,
            bad contact with the media disk, or actual hardware disconnection.
            """)
            report += self._generate_table_for_report(hosts)
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class ConsoleTimeoutSignature(ErrorSignature):
    """
    This signature looks for 'waiting for console' error in cluster's status_info.
    If OpenShift version is 4.7 and hosts are VMware vms, outputs a warning.
    Due to: https://bugzilla.redhat.com/1926345
    """

    def __init__(self, jira_client):
        super().__init__(jira_client, signature_label="console_timeout_VMware_4.7",
                         comment_identifying_string="h1. Console timeout - VMware hosts / OCP 4.7")

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)

        md = get_metadata_json(url)

        def isVMware(host):
            inventory = json.loads(host['inventory'])
            product_name = inventory['system_vendor'].get('product_name')
            return "VMware" in product_name

        cluster = md['cluster']
        status_info = cluster['status_info']
        openshift_version = cluster['openshift_version']
        report = ""
        if ("waiting for console" in status_info and
                "4.7" in openshift_version and
                any(isVMware(host) for host in cluster['hosts'])):
            # ISO conversion according to
            # https://stackoverflow.com/questions/127803/how-do-i-parse-an-iso-8601-formatted-date#comment94022430_49784038
            if datetime.fromisoformat(cluster['install_started_at'].replace('Z', '')) > datetime(2021, 5, 5):
                report = "\n".join([
                    "h2. Waiting for console timeout -possibly due to VMware hosts on OCP 4.7 ([bugzilla|https://bugzilla.redhat.com/1935539])-",
                    "As of May 5th 2021, a fix has been pushed to production. That version contains the supposed fix. "
                    "This means that the cause for the timeout in this ticket might not be due to this known bug - "
                    "it might be caused by something else. Please investigate."
                ])
            else:
                report = "\n".join([
                    "h2. Waiting for console timeout probably due to VMware hosts on OCP 4.7",
                    "You can mark it as caused by issue [MGMT-4454|https://issues.redhat.com/browse/MGMT-4454]",
                    "Solution will be provided via [bugzilla|https://bugzilla.redhat.com/1935539] ticket",
                ])

        if report != "":
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


class AgentStepFailureSignature(Signature):
    """
    This signature creates a report of all agent step failures that were encountered in the logs
    """

    # This has to be maintained to be the same format as
    # https://github.com/openshift/assisted-installer-agent/blob/aebd94105b4ed6442f21a7a26ab4e40eafd936aa/src/commands/step_processor.go#L81
    # Remember to maintain backwards compatibility if that format ever changes - create
    # PATTERN_NEW and try to match both.
    LOG_PATTERN = re.compile(
        r'time="(?P<time>.+)" level=(?P<severity>[a-z]+) msg="(?P<message>Step execution failed.*)" file=.+'
    )

    MSG_PATTERN = re.compile(
        r'Step execution failed \(exit code (?P<exit_code>\d+)\): <(?P<step_id>[a-z\-0-9]+)>, '
        r'command: <(?P<command>.+?)>, args: <\[(?P<args>.+?)\]>\. '
        r'Output:\\nstdout:\\n(?P<stdout>.*)\\n\\nstderr:\\n(?P<stderr>.*)\\n'
    )

    MAX_FAILURES_PER_HOST = 10
    MAX_LINE_LENGTH = 1000
    TRUNCATE_OUTPUT_LINES = 100

    def __init__(self, jira_client):
        super().__init__(jira_client, comment_identifying_string="h1. Agent step failures")

    @classmethod
    def _filter_message(cls, msg) -> bool:
        filtered_strings = ["dhclient was timed out"]
        return any(s in msg['stderr'] for s in filtered_strings)

    @classmethod
    def _prepare_output(cls, output):
        if len(output) == 0:
            return output

        pre = "{code:borderStyle=none|bgColor=transparent} "
        post = " {code}"
        output_lines = output.replace("|", "¦").split("\\n")

        if len(output_lines) > cls.TRUNCATE_OUTPUT_LINES:
            half = cls.TRUNCATE_OUTPUT_LINES // 2
            output_lines = output_lines[:half] + ["**OUTPUT HAS BEEN TRUNCATED**"] + output_lines[-half:]

        output_lines = [line for line in output_lines if line != "\\\""]
        output_lines = [pre + line[:cls.MAX_LINE_LENGTH] + post for line in output_lines]

        return "".join(output_lines)

    def _update_ticket(self, url, issue_key, should_update=False):
        url = self._logs_url_to_api(url)

        md = get_metadata_json(url)

        cluster = md['cluster']
        cluster_id = cluster['id']

        triage_logs_tar = get_triage_logs_tar(triage_url=url, cluster_id=cluster_id)

        report = ''
        for host in cluster["hosts"]:
            host_id = host["id"]

            try:
                agent_logs = get_host_log_file(triage_logs_tar, host_id, "agent.logs")
            except FileNotFoundError:
                continue

            failures = []
            for _failure_idx, step_failure_log_match in zip(range(self.MAX_FAILURES_PER_HOST),
                                                            self.LOG_PATTERN.finditer(agent_logs)):
                step_failure_log = step_failure_log_match.groupdict()
                step_failure_message_match = self.MSG_PATTERN.match(step_failure_log["message"])

                if step_failure_message_match is None:
                    logger.warning("Step failure signature skipped failure because failure message has unexpected format")
                    continue

                step_failure_message = step_failure_message_match.groupdict()

                if not self._filter_message(step_failure_message):
                    failures.append(OrderedDict(
                        time=step_failure_log['time'],
                        exit_code=step_failure_message['exit_code'],
                        step_id=step_failure_message['step_id'],
                        stderr=self._prepare_output(step_failure_message['stderr']),
                    ))

            if len(failures) != 0:
                report += dedent(f"""
                h2. Agent step failures for {host_id} ({self._get_hostname(host)})
                {self._generate_table_for_report(failures)}""")

        if len(report) != 0:
            self._update_triaging_ticket(issue_key, report, should_update=should_update)


############################
# Common functionality
############################
JIRA_SERVER = "https://issues.redhat.com"
SIGNATURES = [AllInstallationAttemptsSignature, ApiInvalidCertificateSignature, FailureDetails,
              FailureDescription, ComponentsVersionSignature, HostsStatusSignature, HostsExtraDetailSignature,
              StorageDetailSignature, InstallationDiskFIOSignature, LibvirtRebootFlagSignature,
              MediaDisconnectionSignature, ConsoleTimeoutSignature, AgentStepFailureSignature,
              CNIConfigurationError]


############################
# Signature runner functionality
############################
LOGS_URL_FROM_DESCRIPTION_OLD = re.compile(r".*logs:\* \[(http.*)\]")
LOGS_URL_FROM_DESCRIPTION_NEW = re.compile(r".*\* \[[lL]ogs\|(http.*)\]")


def search_patterns_in_string(string, patterns):
    if type(patterns) == str:
        patterns = [patterns]

    combined_regex = re.compile(f'({"|".join(r".*%s.*" % pattern for pattern in patterns)})')
    return combined_regex.findall(string)


def group_similar_strings(ls, ratio):
    '''
    Uses Levenshtein Distance Algorithm to calculate the differences between strings
    Then, groups similar strings that matches according to the ratio.
    The higher the ratio -> The more identical strings

    Example:
    input: ['rakesh', 'zakesh', 'goldman LLC', 'oldman LLC', 'bakesh']
    output groups: [['rakesh', 'zakesh', 'bakesh'], ['goldman LLC', 'oldman LLC']]
    '''

    groups = []
    for item in ls:
        for group in groups:
            if all(fuzz.ratio(item, w) > ratio for w in group):
                group.append(item)
                break
        else:
            groups.append([item, ])

    return groups


def get_issue(jclient, issue_key):
    issue = None
    try:
        issue = jclient.issue(issue_key)
    except jira.exceptions.JIRAError as e:
        if e.status_code != 404:
            raise
    if issue is None or issue.fields.components[0].name != "Cloud-Triage":
        raise Exception("issue {} does not exist or is not a triaging issue".format(issue_key))

    return issue


def get_logs_url_from_issue(issue):
    m = LOGS_URL_FROM_DESCRIPTION_NEW.search(issue.fields.description)

    if m is None:
        logger.debug("Cannot find new format of URL for logs in %s", issue.key)
        m = LOGS_URL_FROM_DESCRIPTION_OLD.search(issue.fields.description)
        if m is None:
            logger.debug("Cannot find old format of URL for logs in %s", issue.key)
            return None
    return m.groups()[0]


def get_all_triage_tickets(jclient, only_recent=False):
    recent_filter = "" if not only_recent else 'and created >= -31d'
    query = f"project = AITRIAGE AND component = Cloud-Triage {recent_filter}"

    return jclient.search_issues(query, maxResults=None)


def get_issues(jclient, issue, query=None, only_recent=True):
    if issue is not None:
        logger.info(f"Fetching just {issue}")
        return [get_issue(jclient, issue)]

    if query is not None:
        logger.info(f"Fetching from query '{query}'")
        return jclient.search_issues(query, maxResults=100)

    logger.info(f"Fetching {'recent' if only_recent else 'all'} issues")
    return get_all_triage_tickets(jclient, only_recent=only_recent)


def get_ticket_browse_url(issue_key):
    return f"{JIRA_SERVER}/browse/{issue_key}"


def process_issues(jclient, issues, update, update_signature):
    logger.info(f"Found {len(issues)} tickets, processing...")

    should_progress_bar = sys.stderr.isatty()
    for issue in tqdm.tqdm(issues, disable=not should_progress_bar, file=sys.stderr):
        logger.debug(f"Issue {issue}")
        try:
            url = get_logs_url_from_issue(issue)
        except Exception:
            logger.exception("Error getting logs url of %s", issue.key)
            continue

        if url is None:
            logger.warning(f"Could not get URL from issue {get_ticket_browse_url(issue.key)}. Skipping")
            continue

        add_signatures(jclient, url, issue.key, should_update=update,
                       signatures=update_signature)


def main(args):
    jclient = jira.JIRA(consts.JIRA_SERVER, token_auth=args.jira_access_token)

    issues = get_issues(jclient, args.issue, args.search_query, args.recent_issues)

    if args.dry_run_temp:
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as dry_run_file:
            logger.info(f"Dry run output will be written to {dry_run_file.name}")
            logger.info(f"Run `tail -f {dry_run_file.name}` to view dry run output in real time")
            Signature.dry_run_file = dry_run_file
            process_issues(jclient, issues, args.update, args.update_signature)
            logger.info(f"Dry run output written to {dry_run_file.name}")
        return

    if args.dry_run:
        Signature.dry_run_file = sys.stdout

    process_issues(jclient, issues, args.update, args.update_signature)


def format_time(time_str):
    return dateutil.parser.isoparse(time_str).strftime("%Y-%m-%d %H:%M:%S")


def add_signatures(jclient, url, issue_key, should_update=False, signatures=None):
    name_to_signature = {s.__name__: s for s in SIGNATURES}
    signatures_to_add = SIGNATURES
    if signatures:
        should_update = True
        signatures_to_add = [v for k, v in name_to_signature.items() if k in signatures]

    for sig in signatures_to_add:
        logger.debug(f"Running signature {sig.__name__}")
        s = sig(jclient)
        s.update_ticket(url, issue_key, should_update=should_update)


def parse_args():
    description = f"""
This utility updates existing triage tickets with signatures.
Signatures perform automatic analysis of ticket log files to extract
information that is crucial to help kickstart a ticket triage. Each
signature typically outputs its information in the form of a ticket
comment.


Before running this please make sure you have -

1. A jira username and password -
    Username: You can find it in {JIRA_SERVER}/secure/ViewProfile.jspa
    Password: Simply your sso.redhat.com password

2. A RedHat VPN connection - this is required to access ticket log files

3. A Python virtualenv with all the requirements.txt installed
   (you can also install them without a virtualenv if you wish).

You can run this script without affecting the tickets by using the --dry-run flag
""".strip()

    signature_names = [s.__name__ for s in SIGNATURES]
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--jira-access-token", default=os.environ.get("JIRA_ACCESS_TOKEN"), required=False,
                        help="PAT (personal access token) for accessing Jira")

    selectors_group = parser.add_argument_group(title="Issues selection")
    selectors = selectors_group.add_mutually_exclusive_group(required=True)
    selectors.add_argument("-r", "--recent-issues", action='store_true', help="Handle recent (30 days) Triaging Tickets")
    selectors.add_argument("-a", "--all-issues", action='store_true', help="Handle all Triaging Tickets")
    selectors.add_argument("-i", "--issue", required=False, help="Triage issue key")
    selectors.add_argument("-s", "--search-query", required=False, help="Triage issue jql query")

    parser.add_argument("-u", "--update", action="store_true", help="Update ticket even if signature already exist")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output verbose logging")

    dry_run_group = parser.add_argument_group(title="Dry run options")
    dry_run_args = dry_run_group.add_mutually_exclusive_group()
    dry_run_msg = "Dry run. Don't update tickets. Write output to"
    dry_run_args.add_argument("-d", "--dry-run", action="store_true", help=f"{dry_run_msg} stdout")
    dry_run_args.add_argument("-t", "--dry-run-temp", action="store_true", help=f"{dry_run_msg} a temp file")

    parser.add_argument("-us", "--update-signature", action='append', choices=signature_names,
                        help="Update tickets with only the signatures specified")

    args = parser.parse_args()

    config_logger(args.verbose)

    return args


if __name__ == "__main__":
    main(parse_args())

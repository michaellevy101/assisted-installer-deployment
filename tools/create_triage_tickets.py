#!/usr/bin/env python3

# This script gets a list of the filed clusters from the assisted-logs-server
# For each cluster, which does not already has a triaging Jira ticket, it creates one

import os
import argparse
import logging
import sys
from retry import retry

import jira
import requests

import close_by_signature
from add_triage_signature import FailureDescription, days_ago, add_signatures, custom_field_name, CF_DOMAIN, CF_IGNORED_DOMAINS
import consts


DEFAULT_DAYS_TO_HANDLE = 30
DEFAULT_WATCHERS = ["mkowalsk"]


LOGS_COLLECTOR = "http://assisted-logs-collector.usersys.redhat.com"
JIRA_SUMMARY = "cloud.redhat.com failure: {failure_id}"


def format_summary(failure_data):
    return JIRA_SUMMARY.format(**failure_data)


def get_all_triage_tickets(jclient):
    query = 'component = "Cloud-Triage"'
    idx = 0
    block_size = 100
    summaries, issues = [], []
    while True:
        issues_bulk = jclient.search_issues(
            query,
            maxResults=block_size,
            startAt=idx,
            fields=['summary', 'key', 'status'],
        )
        if len(issues_bulk) == 0:
            break
        summaries.extend([x.fields.summary for x in issues_bulk])
        issues.extend(issues_bulk)
        idx += block_size

    return issues, set(summaries)


def add_watchers(jclient, issue):
    for watcher in DEFAULT_WATCHERS:
        jclient.add_watcher(issue.key, watcher)


def close_custom_domain_user_ticket(jclient, issue_key):
    issue = jclient.issue(issue_key)
    if issue.raw['fields'].get(custom_field_name(CF_DOMAIN)) in CF_IGNORED_DOMAINS:
        logger.info("closing custom user's issue: %s", issue_key)
        jclient.transition_issue(issue, close_by_signature.TARGET_TRANSITION_ID)
        jclient.add_comment(issue, "Automatically closing the issue for the specified domain.")


def create_jira_ticket(jclient, existing_tickets, failure_id, cluster_md):
    summary = format_summary({"failure_id": failure_id})
    if summary in existing_tickets:
        logger.debug("issue found: %s", summary)
        return None

    url = "{}/files/{}".format(LOGS_COLLECTOR, failure_id)

    major, minor, *_ = cluster_md['openshift_version'].split(".")
    ocp_key = f"{major}.{minor}"

    ticket_affected_version_field = 'OpenShift {}'.format(ocp_key)
    new_issue = jclient.create_issue(project="AITRIAGE",
                                     summary=summary,
                                     versions=[{'name': ticket_affected_version_field}],
                                     components=[{'name': "Cloud-Triage"}],
                                     priority={'name': 'Blocker'},
                                     issuetype={'name': 'Bug'},
                                     description=FailureDescription(jclient).build_description(
                                         url,
                                         cluster_md))

    logger.info("issue created: %s", new_issue)
    # (mko 27/10/2021) Disabling adding watchers due to the HTTP 400 error raised when creating
    #                  AITRIAGE tickets from Jenkins using this script.
    # add_watchers(jclient, new_issue)
    return new_issue


@retry(exceptions=jira.exceptions.JIRAError, tries=3, delay=10)
def main(args):
    jclient = jira.JIRA(consts.JIRA_SERVER, token_auth=args.jira_access_token)

    try:
        res = requests.get("{}/files/".format(LOGS_COLLECTOR))
    except Exception:
        logger.exception("Error getting list of failed clusters")
        sys.exit(1)

    res.raise_for_status()
    failed_clusters = res.json()

    issues, summaries = get_all_triage_tickets(jclient)
    if not issues:
        raise ConnectionError("Failed to get any issues from JIRA")

    for failure in failed_clusters:
        date = failure["name"].split("_")[0]
        if not args.all and days_ago(date) > DEFAULT_DAYS_TO_HANDLE:
            continue

        res = requests.get("{}/files/{}/metadata.json".format(LOGS_COLLECTOR, failure['name']))
        res.raise_for_status()
        cluster = res.json()['cluster']

        if cluster['status'] == "error":
            new_issue = create_jira_ticket(jclient, summaries, failure['name'], cluster)
            if new_issue is not None:
                logs_url = "{}/files/{}".format(LOGS_COLLECTOR, failure['name'])
                add_signatures(jclient, logs_url, new_issue.key)
                close_custom_domain_user_ticket(jclient, new_issue.key)

    if not args.filters_json:
        return

    close_by_signature.run_using_json(args.filters_json, jclient, issues)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    loginGroup = parser.add_argument_group(title="login options")
    loginArgs = loginGroup.add_mutually_exclusive_group()
    loginArgs.add_argument("--jira-access-token", default=os.environ.get("JIRA_ACCESS_TOKEN"), required=False,
                           help="PAT (personal access token) for accessing Jira")
    parser.add_argument("-a", "--all", action="store_true",
                        help="Try creating Triage Tickets for all failures. " +
                        "Default is just for failures in the past 30 days")
    parser.add_argument("-v", "--verbose", action="store_true", help="Output verbose logging")
    parser.add_argument(
        '--filters-json',
        help='At the end of the run, filter and close issues that applied to '
             'the rules in a given json file which has the format: '
             '{signature_type: {root_issue: message}}',
        default='./triage_resolving_filters.json',
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARN, format='%(levelname)-10s %(message)s')
    logger = logging.getLogger(__name__)
    logging.getLogger("__main__").setLevel(logging.INFO)

    if args.verbose:
        logging.getLogger("__main__").setLevel(logging.DEBUG)

    main(args)

from __future__ import absolute_import

import dateutil.parser
import logging
import six
import re

import ipaddress

from django.db import IntegrityError, transaction
from django.http import HttpResponse, Http404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View
from django.utils import timezone
from simplejson import JSONDecodeError
from sentry.models import (
    Commit, CommitAuthor, Organization, Repository
)
from sentry.plugins.providers import RepositoryProvider
from sentry.utils import json

logger = logging.getLogger('sentry.webhooks')

# Bitbucket Cloud IP range: https://confluence.atlassian.com/bitbucket/manage-webhooks-735643732.html#Managewebhooks-trigger_webhookTriggeringwebhooks
BITBUCKET_IP_RANGE = ipaddress.ip_network(u'104.192.143.0/24')


class Webhook(object):
    def __call__(self, organization, event):
        raise NotImplementedError


def parse_raw_user(raw):
    # captures content between angle brackets
    return re.search('(?<=<).*(?=>$)', raw).group(0)


class PushEventWebhook(Webhook):
    # https://confluence.atlassian.com/bitbucket/event-payloads-740262817.html#EventPayloads-Push
    def __call__(self, organization, event):
        authors = {}

        try:
            repo = Repository.objects.get(
                organization_id=organization.id,
                provider='bitbucket',
                external_id=six.text_type(event['repository']['uuid']),
            )
        except Repository.DoesNotExist:
            raise Http404()

        if repo.config.get('name') != event['repository']['full_name']:
            repo.config['name'] = event['repository']['full_name']
            repo.save()

        for change in event['push']['changes']:
            for commit in change.get('commits', []):
                if RepositoryProvider.should_ignore_commit(commit['message']):
                    continue

                author_email = parse_raw_user(commit['author']['raw'])

                # TODO(dcramer): we need to deal with bad values here, but since
                # its optional, lets just throw it out for now
                if len(author_email) > 75:
                    author = None
                elif author_email not in authors:
                    authors[author_email] = author = CommitAuthor.objects.get_or_create(
                        organization_id=organization.id,
                        email=author_email,
                        defaults={
                            'name': commit['author']['raw'].split('<')[0].strip()
                        }
                    )[0]
                else:
                    author = authors[author_email]
                try:
                    with transaction.atomic():

                        Commit.objects.create(
                            repository_id=repo.id,
                            organization_id=organization.id,
                            key=commit['hash'],
                            message=commit['message'],
                            author=author,
                            date_added=dateutil.parser.parse(
                                commit['date'],
                            ).astimezone(timezone.utc),
                        )

                except IntegrityError:
                    pass


class BitbucketWebhookEndpoint(View):
    _handlers = {
        'repo:push': PushEventWebhook,
    }

    def get_handler(self, event_type):
        return self._handlers.get(event_type)

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        if request.method != 'POST':
            return HttpResponse(status=405)

        return super(BitbucketWebhookEndpoint, self).dispatch(request, *args, **kwargs)

    def post(self, request, organization_id):
        try:
            organization = Organization.objects.get_from_cache(
                id=organization_id,
            )
        except Organization.DoesNotExist:
            logger.error('bitbucket.webhook.invalid-organization', extra={
                'organization_id': organization_id,
            })
            return HttpResponse(status=400)

        body = six.binary_type(request.body)
        if not body:
            logger.error('bitbucket.webhook.missing-body', extra={
                'organization_id': organization.id,
            })
            return HttpResponse(status=400)

        try:
            handler = self.get_handler(request.META['HTTP_X_EVENT_KEY'])
        except KeyError:
            logger.error('bitbucket.webhook.missing-event', extra={
                'organization_id': organization.id,
            })
            return HttpResponse(status=400)

        if not handler:
            return HttpResponse(status=204)

        if not ipaddress.ip_address(six.text_type(request.META['REMOTE_ADDR'])) in BITBUCKET_IP_RANGE:
            logger.error('bitbucket.webhook.invalid-ip-range', extra={
                'organization_id': organization.id,
            })
            return HttpResponse(status=401)

        try:
            event = json.loads(body.decode('utf-8'))
        except JSONDecodeError:
            logger.error('bitbucket.webhook.invalid-json', extra={
                'organization_id': organization.id,
            }, exc_info=True)
            return HttpResponse(status=400)

        handler()(organization, event)
        return HttpResponse(status=204)

"""Pure GitHub observation classification, never authentication or execution.

Inputs are untrusted caller-supplied JSON bytes. Numeric actor/grammar checks
recognize candidates only. No loader, grant, dispatch or readiness API exists
here; those are unimplemented dependencies, not implied by this adapter.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

PARSER_VERSION = 'github-review-observation-v1'
ACTORS = {
    136622811: ('coderabbitai[bot]', 'coderabbit'),
    199175422: ('chatgpt-codex-connector[bot]', 'chatgpt'),
}
MAX_BYTES = 1024 * 1024
SHA = r'[0-9a-f]{40}'
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
QUOTA_START = ('<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n'
               '<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n\n'
               '> [!WARNING]\n> ## Review limit reached\n')
QUOTA_END = '<!-- end of auto-generated comment: rate limited by coderabbit.ai -->'
REPLY_PATTERN = (
    r'<!-- This is an auto-generated reply by CodeRabbit -->\n'
    r'<!-- CodeRabbit review command invocation: v2:([0-9a-f]{64}) -->\n'
    r'<details>\n<summary>⚠️ Action not completed</summary>\n\nReview rate limited\.\n\n'
    r'> Note: CodeRabbit is an incremental review system and does not re-review already reviewed commits\. '
    r'This command is applicable only when automatic reviews are paused\.\n\n</details>')
REVIEW_STATES = {'COMMENTED', 'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED', 'PENDING'}
QUOTA_LINES = {
    '', '>', '> ', '> <details>', '> </details>',
    '> <summary>View limit details</summary>',
    '> **Limit details:** You’ve used the included review currently available.',
    "> You've used all free OSS reviews for now. Wait for the free limit to reset to keep reviewing this public repository.",
    '> [Learn how review limits work](https://docs.coderabbit.ai/management/plans#rate-limits).',
    '> **Review configuration:**', '> <summary>⚙️ Run configuration</summary>',
    '> **Configuration used**: defaults', '> **Review profile**: CHILL', '> **Plan**: Team',
    '> <summary>📥 Commits</summary>',
}
QUOTA_LINE_PATTERNS = (
    r'> \*\*Next included review available in [0-9]{1,5} minutes?\.\*\*',
    r'> \[Check out review usage here\]\(https://app\.coderabbit\.ai/dashboard/review-capacity\?orgId=' + UUID + r'\)\.',
    r'> \*\*Run ID\*\*: `' + UUID + r'`',
    r'> Reviewing files that changed from the base of the PR and between ' + SHA + r' and ' + SHA + r'\.',
    r'> <summary>📒 Files selected for processing \([0-9]{1,5}\)</summary>',
    r'> \* `(?!/)(?!.*(?:^|/)\.\.(?:/|`))[A-Za-z0-9_./-]+`',
)


def _known_tips(tail: str) -> bool:
    if not tail.strip():
        return True
    # Fixed marketing structure from the observed provider version. URL query
    # values are opaque data; arbitrary prose or additional sections are not.
    prefix = ('\n\n<!-- tips_start -->\n\n---\n\nThanks for using [CodeRabbit](')
    prose = (")! It's free for OSS, and your support helps us grow. If you like it, consider giving us a shout-out."
             '\n\n<details>\n<summary>❤️ Share</summary>\n\n')
    pattern = (re.escape(prefix)
               + r'https://coderabbit\.ai\?utm_source=oss&utm_medium=github&utm_campaign=[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+&utm_content=[0-9]+'
               + re.escape(prose))
    links = [('X', 'https://twitter.com/intent/tweet?'),
             ('Mastodon', 'https://mastodon.social/share?'),
             ('Reddit', 'https://www.reddit.com/submit?'),
             ('LinkedIn', 'https://www.linkedin.com/sharing/share-offsite/?')]
    for label, url in links:
        pattern += re.escape(f'- [{label}]({url}') + r'[^\s()<>]+' + re.escape(')\n')
    pattern += re.escape('\n</details>\n\n\n<sub>Comment `@coderabbitai help` to get the list of available commands.</sub>\n\n<!-- tips_end -->')
    return re.fullmatch(pattern, tail) is not None


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 2048 and bool(value.strip())


def _id(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**53 - 1


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(SHA, value) is not None


def _time(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        return False
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    return True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _quota(body: str) -> dict[str, Any] | None:
    reply = re.fullmatch(REPLY_PATTERN, body)
    if reply:
        return dict(request_ref='coderabbit-command:v2:' + reply[1], base=None, head=None)
    if not body.startswith(QUOTA_START) or body.count(QUOTA_END) != 1:
        return None
    section, tail = body.split(QUOTA_END)
    # Only the provider's quoted quota stanza is recognized. Mixed review/skip
    # sections, duplicate markers and fenced examples never qualify.
    if any(marker in body for marker in ('```', '<!-- recent_review', '<!-- walkthrough',
                                         '<!-- This is an auto-generated comment: skip review')):
        return None
    if body.count(QUOTA_START) != 1:
        return None
    quoted = section[len(QUOTA_START):].splitlines()
    if any(line not in QUOTA_LINES and not any(re.fullmatch(pattern, line)
           for pattern in QUOTA_LINE_PATTERNS) for line in quoted):
        return None
    if not _known_tips(tail):
        return None
    runs = re.findall(r'^> \*\*Run ID\*\*: `(' + UUID + r')`$', section, re.MULTILINE)
    commits = re.findall(r'^> Reviewing files that changed from the base of the PR and between ('
                         + SHA + r') and (' + SHA + r')\.$', section, re.MULTILINE)
    if len(runs) > 1 or len(commits) > 1:
        return None
    return dict(request_ref='coderabbit-run:' + runs[0] if runs else None,
                base=commits[0][0] if commits else None, head=commits[0][1] if commits else None)


def classify_observation(raw_json: bytes, *, expected: dict[str, Any]) -> dict[str, Any]:
    """Return a versioned, inert classification with unresolved associations.

    expected is a comparison target, never a trusted grant. A candidate_match
    means lexical identity agreement only. It does not authenticate raw_json.
    """
    result: dict[str, Any] = dict(parser_version=PARSER_VERSION, classification='unclassified',
        authentication_status='integration_pending', association_status='pending', pending_reasons=[],
        provider=None, actor_id=None, observation_id=None, observation_key=None, raw_digest=None,
        updated_at=None, submitted_at=None, request_ref=None, review_state=None,
        snapshot=dict(repository=None, base=None, head=None))
    reasons = result['pending_reasons']
    if not isinstance(raw_json, bytes) or len(raw_json) > MAX_BYTES:
        reasons.append('raw_input_invalid')
        return result
    result['raw_digest'] = hashlib.sha256(raw_json).hexdigest()
    fields = {'repository', 'pr', 'base', 'head', 'request_ref'}
    if (not isinstance(expected, dict) or set(expected) != fields
            or not isinstance(expected['repository'], str)
            or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', expected['repository']) is None
            or not _id(expected['pr']) or not _sha(expected['base']) or not _sha(expected['head'])
            or not (expected['request_ref'] is None or _text(expected['request_ref']))):
        reasons.append('expected_identity_invalid')
        return result
    try:
        row = json.loads(raw_json, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        reasons.append('raw_json_invalid')
        return result
    if not isinstance(row, dict) or not _id(row.get('id')) or not isinstance(row.get('body'), str):
        reasons.append('observation_invalid')
        return result
    actor = row.get('user')
    if (not isinstance(actor, dict) or not _id(actor.get('id')) or actor['id'] not in ACTORS
            or actor.get('login') != ACTORS[actor['id']][0] or actor.get('type') != 'Bot'):
        reasons.append('actor_mismatch')
        return result
    result.update(provider=ACTORS[actor['id']][1], actor_id=actor['id'], observation_id=row['id'])
    is_review = result['provider'] == 'chatgpt' and 'state' in row and 'commit_id' in row
    stamp = row.get('submitted_at') if is_review else row.get('updated_at')
    if not _time(stamp) or (not is_review and not _time(row.get('created_at'))):
        reasons.append('observation_time_invalid')
        return result
    result['submitted_at' if is_review else 'updated_at'] = stamp
    fragment = ('pullrequestreview-' if is_review else 'issuecomment-') + str(row['id'])
    url = f"https://github.com/{expected['repository']}/pull/{expected['pr']}#{fragment}"
    if row.get('html_url') != url:
        reasons.append('resource_identity_mismatch')
    result['snapshot']['repository'] = expected['repository'] if row.get('html_url') == url else None
    result['observation_key'] = hashlib.sha256(json.dumps(
        [row.get('html_url'), actor['id'], row['id'], stamp, result['raw_digest']],
        separators=(',', ':')).encode()).hexdigest()
    if result['provider'] == 'coderabbit':
        parsed = _quota(row['body'])
        if parsed is None:
            result['classification'] = 'not_quota'
        else:
            result.update(classification='quota_candidate', request_ref=parsed['request_ref'])
            result['snapshot'].update(base=parsed['base'], head=parsed['head'])
    elif is_review and isinstance(row['state'], str) and row['state'] in REVIEW_STATES and _sha(row['commit_id']):
        result.update(classification='review_observed', review_state=row['state'])
        result['snapshot']['head'] = row['commit_id']
        # Review API has no request ID or historical base. Never fill these
        # from the current PR snapshot or turn COMMENTED/APPROVED into pass.
    else:
        reasons.append('review_identity_missing')
    for field in ('base', 'head', 'request_ref'):
        observed = result['request_ref'] if field == 'request_ref' else result['snapshot'][field]
        if expected[field] is None:
            reasons.append('expected_' + field + '_missing')
        if observed is None:
            reasons.append(field + '_missing')
        elif expected[field] is not None and observed != expected[field]:
            reasons.append(field + '_mismatch')
    if not reasons and result['classification'] in ('quota_candidate', 'review_observed'):
        result['association_status'] = 'candidate_match'
    return result

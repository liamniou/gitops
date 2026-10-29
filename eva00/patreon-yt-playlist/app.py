"""Collect YouTube videos posted on Patreon (by creators you're a member of) into a YouTube playlist.

Every INTERVAL_MINUTES:
  1. list campaigns from your active Patreon memberships (plus EXTRA_CAMPAIGN_IDS)
  2. scan each campaign's posts newer than the cutoff (BACKFILL_DAYS on first run, LOOKBACK_DAYS afterwards)
  3. extract YouTube video IDs from the post embed/content and append unseen ones to the playlist (oldest first)

Env:
  PATREON_SESSION_ID   value of the patreon.com `session_id` cookie
  YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN   Google OAuth "Desktop app" client + refresh token
                       (scope youtube); get the token with `python app.py auth`
  PLAYLIST_ID          optional; otherwise a playlist PLAYLIST_TITLE/PLAYLIST_PRIVACY is created once
  DISCORD_WEBHOOK_URL  optional; notifications about added videos and auth failures
"""
import datetime as dt
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DATA_DIR = os.environ.get('DATA_DIR', '/data')
INTERVAL_MINUTES = int(os.environ.get('INTERVAL_MINUTES', '720'))
RETRY_MINUTES = int(os.environ.get('RETRY_MINUTES', '30'))
BACKFILL_DAYS = int(os.environ.get('BACKFILL_DAYS', '180'))
LOOKBACK_DAYS = int(os.environ.get('LOOKBACK_DAYS', '14'))
PLAYLIST_ID = os.environ.get('PLAYLIST_ID', '').strip()
PLAYLIST_TITLE = os.environ.get('PLAYLIST_TITLE', 'Patreon')
PLAYLIST_PRIVACY = os.environ.get('PLAYLIST_PRIVACY', 'unlisted')
EXTRA_CAMPAIGN_IDS = [c.strip() for c in os.environ.get('EXTRA_CAMPAIGN_IDS', '').split(',') if c.strip()]
EXCLUDE_CAMPAIGN_IDS = {c.strip() for c in os.environ.get('EXCLUDE_CAMPAIGN_IDS', '').split(',') if c.strip()}
MAX_ATTEMPTS = int(os.environ.get('MAX_ATTEMPTS', '3'))
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()

PATREON = 'https://www.patreon.com/api'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
YT = 'https://www.googleapis.com/youtube/v3'
YT_SCOPE = 'https://www.googleapis.com/auth/youtube'
YT_ID_RE = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^"\'\s<>]*?&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('patreon-yt-playlist')


class AuthError(Exception):
    pass


class QuotaExceeded(Exception):
    pass


class YouTubeError(Exception):
    pass


def http(method, url, headers=None, body=None, form=None, timeout=60):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'raw': raw[:300].decode(errors='replace')}


def notify(text):
    if not DISCORD_WEBHOOK_URL:
        return
    for i in range(0, len(text), 1900):
        status, resp = http('POST', DISCORD_WEBHOOK_URL, headers={'User-Agent': UA}, body={'content': text[i:i + 1900]})
        if status >= 300:
            log.warning('Discord notify failed: %s %s', status, resp)


# ---------------------------------------------------------------- state

def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(os.path.join(DATA_DIR, 'state.db'))
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            post_id TEXT, post_url TEXT, post_title TEXT, creator TEXT, published_at TEXT,
            error TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
    ''')
    return conn


def kv_get(conn, k):
    row = conn.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
    return row[0] if row else None


def kv_set(conn, k, v):
    conn.execute('INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v))
    conn.commit()


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


# ---------------------------------------------------------------- Patreon

def patreon_get(path, params):
    url = f'{PATREON}/{path}?' + urllib.parse.urlencode({**params, 'json-api-version': '1.0'})
    headers = {'User-Agent': UA, 'Accept': 'application/vnd.api+json',
               'Cookie': f'session_id={os.environ["PATREON_SESSION_ID"]}'}
    for attempt in range(4):
        status, resp = http('GET', url, headers=headers)
        if status == 200:
            return resp
        if status in (401, 403) and 'errors' in resp:
            raise AuthError(f'Patreon {status}: {resp["errors"][0].get("title")} (session_id cookie expired?)')
        if status in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(10 * (attempt + 1))
            continue
        raise RuntimeError(f'Patreon GET {path} -> {status}: {str(resp)[:300]}')


def patreon_campaigns():
    resp = patreon_get('current_user', {
        'include': 'active_memberships.campaign',
        'fields[member]': 'patron_status',
        'fields[campaign]': 'name,url',
        'fields[user]': 'full_name',
    })
    log.info('Patreon user: %s', resp['data']['attributes'].get('full_name'))
    campaigns = {i['id']: i['attributes'].get('name') or i['id']
                 for i in resp.get('included', []) if i['type'] == 'campaign'}
    for cid in EXTRA_CAMPAIGN_IDS:
        campaigns.setdefault(cid, cid)
    return {cid: name for cid, name in campaigns.items() if cid not in EXCLUDE_CAMPAIGN_IDS}


def patreon_posts(campaign_id, cutoff):
    params = {
        'filter[campaign_id]': campaign_id,
        'filter[contains_exclusive_posts]': 'true',
        'filter[is_draft]': 'false',
        'sort': '-published_at',
        'fields[post]': 'title,url,published_at,embed,content,post_type,current_user_can_view',
        'json-api-use-default-includes': 'false',
        'page[count]': '50',
    }
    while True:
        resp = patreon_get('posts', params)
        for post in resp.get('data', []):
            a = post['attributes']
            if not a.get('published_at') or dt.datetime.fromisoformat(a['published_at']) < cutoff:
                return
            yield post['id'], a
        nxt = (resp.get('links') or {}).get('next')
        if not nxt:
            return
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(nxt).query))


def youtube_ids(attrs):
    embed = attrs.get('embed') or {}
    text = ' '.join(filter(None, [embed.get('url'), embed.get('html'), html.unescape(attrs.get('content') or '')]))
    return list(dict.fromkeys(YT_ID_RE.findall(text)))


# ---------------------------------------------------------------- YouTube

class YouTube:
    def __init__(self):
        self.token = None
        self.expires = 0

    def _auth(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        status, resp = http('POST', 'https://oauth2.googleapis.com/token', form={
            'client_id': os.environ['YT_CLIENT_ID'], 'client_secret': os.environ['YT_CLIENT_SECRET'],
            'refresh_token': os.environ['YT_REFRESH_TOKEN'], 'grant_type': 'refresh_token'})
        if status != 200:
            raise AuthError(f'Google token refresh failed {status}: {resp} (re-run `python app.py auth`)')
        self.token, self.expires = resp['access_token'], time.time() + resp.get('expires_in', 3600)
        return self.token

    def call(self, method, path, params=None, body=None):
        url = f'{YT}/{path}?' + urllib.parse.urlencode(params or {})
        status, resp = http(method, url, headers={'Authorization': f'Bearer {self._auth()}'}, body=body)
        if status < 300:
            return resp
        err = resp.get('error') or {}
        reasons = [e.get('reason') for e in err.get('errors', [])]
        if 'quotaExceeded' in reasons or 'rateLimitExceeded' in reasons:
            raise QuotaExceeded(reasons)
        if status == 401:
            raise AuthError(f'YouTube 401: {resp}')
        raise YouTubeError(f'{status} {reasons} {err.get("message") or str(resp)[:300]}')

    def ensure_playlist(self, conn):
        """Returns (playlist_id, created_now)."""
        pid = PLAYLIST_ID or kv_get(conn, 'playlist_id')
        if pid:
            return pid, False
        resp = self.call('POST', 'playlists', {'part': 'snippet,status'}, {
            'snippet': {'title': PLAYLIST_TITLE, 'description': 'Videos posted by Patreon creators (auto-collected)'},
            'status': {'privacyStatus': PLAYLIST_PRIVACY}})
        kv_set(conn, 'playlist_id', resp['id'])
        log.info('Created playlist %s https://www.youtube.com/playlist?list=%s', PLAYLIST_TITLE, resp['id'])
        notify(f'Created YouTube playlist **{PLAYLIST_TITLE}**: https://www.youtube.com/playlist?list={resp["id"]}')
        return resp['id'], True

    def playlist_video_ids(self, pid):
        ids, params = set(), {'part': 'contentDetails', 'playlistId': pid, 'maxResults': 50}
        while True:
            for attempt in range(4):
                try:
                    resp = self.call('GET', 'playlistItems', params)
                    break
                except YouTubeError as e:
                    # A freshly created playlist can be briefly invisible to playlistItems.list.
                    if 'playlistNotFound' not in str(e) or attempt == 3:
                        raise
                    time.sleep(15)
            ids.update(i['contentDetails']['videoId'] for i in resp.get('items', []))
            if not resp.get('nextPageToken'):
                return ids
            params['pageToken'] = resp['nextPageToken']

    def add(self, pid, video_id):
        self.call('POST', 'playlistItems', {'part': 'snippet'}, {
            'snippet': {'playlistId': pid, 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}})


# ---------------------------------------------------------------- sync

def sync(conn, yt):
    first_run = kv_get(conn, 'last_success') is None
    days = BACKFILL_DAYS if first_run else LOOKBACK_DAYS
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    found = {}
    for cid, name in patreon_campaigns().items():
        n_posts = n_videos = 0
        for post_id, a in patreon_posts(cid, cutoff):
            n_posts += 1
            if a.get('current_user_can_view') is False:
                continue
            for vid in youtube_ids(a):
                n_videos += 1
                prev = found.get(vid)
                if not prev or a['published_at'] < prev['published_at']:
                    found[vid] = {'post_id': post_id, 'post_url': a.get('url'), 'post_title': a.get('title'),
                                  'creator': name, 'published_at': a['published_at']}
        log.info('%s (%s): %d posts, %d YouTube links in last %d days', name, cid, n_posts, n_videos, days)

    done = {r[0]: r[1:] for r in conn.execute('SELECT video_id, status, attempts FROM videos')}
    todo = sorted(((vid, m) for vid, m in found.items()
                   if vid not in done or (done[vid][0] == 'failed' and done[vid][1] < MAX_ATTEMPTS)),
                  key=lambda x: x[1]['published_at'])
    log.info('%d YouTube videos found, %d to add', len(found), len(todo))
    if not todo:
        kv_set(conn, 'last_success', now_iso())
        return

    pid, created = yt.ensure_playlist(conn)
    existing = set() if created else yt.playlist_video_ids(pid)
    added = []
    try:
        for vid, m in todo:
            status, error = 'added', None
            if vid in existing:
                status = 'present'
            else:
                try:
                    yt.add(pid, vid)
                    existing.add(vid)
                    added.append((vid, m))
                    log.info('Added %s [%s] %s', vid, m['creator'], m['post_title'])
                except YouTubeError as e:
                    status, error = 'failed', str(e)
                    log.warning('Failed %s [%s] %s: %s', vid, m['creator'], m['post_title'], e)
            conn.execute('''INSERT INTO videos(video_id, status, attempts, post_id, post_url, post_title, creator,
                                               published_at, error, updated_at)
                            VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(video_id) DO UPDATE SET status=excluded.status, attempts=videos.attempts + 1,
                                error=excluded.error, updated_at=excluded.updated_at''',
                         (vid, status, m['post_id'], m['post_url'], m['post_title'], m['creator'],
                          m['published_at'], error, now_iso()))
            conn.commit()
    except QuotaExceeded:
        # Don't mark the run successful during the first backfill, so the next run rescans the full window.
        log.warning('YouTube quota exhausted; remaining videos will be added next run')
        if first_run:
            return
    finally:
        if added:
            lines = [f'- [{m["creator"]}] {m["post_title"]} https://youtu.be/{vid}' for vid, m in added]
            notify(f'Added {len(added)} Patreon video(s) to https://www.youtube.com/playlist?list={pid}\n'
                   + '\n'.join(lines))
    kv_set(conn, 'last_success', now_iso())


def run_forever():
    conn, yt = db(), YouTube()
    auth_alerted = False
    while True:
        sleep_minutes = INTERVAL_MINUTES
        try:
            sync(conn, yt)
            auth_alerted = False
        except AuthError as e:
            log.error('%s', e)
            if not auth_alerted:
                notify(f':warning: patreon-yt-playlist: {e}')
                auth_alerted = True
        except Exception:
            log.exception('Sync failed')
            sleep_minutes = min(INTERVAL_MINUTES, RETRY_MINUTES)
        log.info('Next sync in %d min', sleep_minutes)
        time.sleep(sleep_minutes * 60)


# ---------------------------------------------------------------- one-off OAuth helper

def oauth_cli():
    """Prints a consent URL; paste back the URL the browser was redirected to (it fails to load, that's fine)."""
    redirect = 'http://localhost:8765/'
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': os.environ['YT_CLIENT_ID'], 'redirect_uri': redirect, 'response_type': 'code',
        'scope': YT_SCOPE, 'access_type': 'offline', 'prompt': 'consent'})
    print(f'Open:\n{url}\n')
    reply = input('Paste the redirected URL (or just the code): ').strip()
    code = urllib.parse.parse_qs(urllib.parse.urlsplit(reply).query).get('code', [reply])[0]
    status, resp = http('POST', 'https://oauth2.googleapis.com/token', form={
        'client_id': os.environ['YT_CLIENT_ID'], 'client_secret': os.environ['YT_CLIENT_SECRET'],
        'code': code, 'redirect_uri': redirect, 'grant_type': 'authorization_code'})
    if status != 200 or 'refresh_token' not in resp:
        sys.exit(f'Token exchange failed {status}: {resp}')
    print(f'\nYT_REFRESH_TOKEN={resp["refresh_token"]}')


if __name__ == '__main__':
    if sys.argv[1:] == ['auth']:
        oauth_cli()
    elif sys.argv[1:] == ['once']:
        sync(db(), YouTube())
    else:
        run_forever()

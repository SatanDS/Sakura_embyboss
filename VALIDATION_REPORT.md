# Validation Report - 2026-09-09

## Tested Version

- Git base: `a4ad8da` plus the fixes and regression tests delivered with this report.
- The local base was initially `4bbc671`. Seven already-published commits were
  restored before the final VIP tests, including the read-only Emby token DB
  mapping and README section 16.2. The pre-sync failures are not attributed to
  the restored upstream implementation.
- Target: Debian 13.5 amd64, 1682 MiB RAM, temporary 2 GiB swap.
- Runtime: Python 3.10.11 Alpine image, Docker 26.1.5, MySQL 5.7.44,
  Emby 4.10.0.40, Caddy 2.x. Test services are isolated from production.
- The initial image was `tgbot-validation:final`; the release candidate is
  rebuilt after the positive HLS compatibility fix. Target build logs retain
  the image ID and source archive hash.

## Results

| Check | Result |
| --- | --- |
| Offline suite, Windows and release Alpine image | 118 passed, 34 opt-in checks skipped |
| Real MySQL migration and transaction suite | 8 passed |
| Real Emby account and library policy suite | 6 passed |
| Real Caddy -> Bot -> MySQL/Emby VIP matrix, final image | 14 passed |
| Source/gateway positive media suite | 7 passed |
| Real registration queue with disposable MySQL/Emby | 1 passed, 3 accounts created and removed |
| Caddy forwarding and Nginx syntax suite | 2 passed |
| Docker build and runtime-file exclusion | Passed; config and 11 canaries absent |
| Whitespace and Python syntax checks | Passed |

Counts from separate suites overlap in configuration-only checks; they are not
an additive count of unique tests. Network opt-in checks were run separately
against the disposable services. Telegram message delivery was mocked.

## VIP Findings Fixed And Retested

- Media URLs without `/emby` reached actual video bytes without line validation.
  The Caddy matchers now include both prefixes, case variants, arbitrary video
  and audio stream filenames, HLS, downloads, playback-info and live routes.
- With automatic violation banning enabled, a MySQL outage disabled a valid VIP
  account. The same outage now returns HTTP 503 without disabling the account,
  terminating its session, or entering the violation cooldown.
- A revoked authoritative token could fall back to an old Sessions.AccessToken.
  A configured auth database now has final authority; stale sessions cannot
  restore a rejected identity.
- Duplicate token rows and incomplete numeric-ID mappings could hide ambiguity.
  These cases now reject authorization and have SQLite regressions.
- Source-generated HLS segments used only PlaySessionId. The source returned
  200 while the gateway returned 401. The Bot now remembers verified VIP
  manifests, scoped to the host, playback ID, media kind and media ID. Each
  tokenless segment revalidates the original token and current entitlement.
  Unknown sessions, mismatched media/host, invalid explicit credentials,
  revoked tokens and expired VIP access are denied. Ordinary-line sessions
  cannot be reused on the VIP line.

Punitive-mode target checks also passed: a valid VIP with a stale client UserId
remained playable; an invalid token did not punish the claimed victim; a normal
user forging a VIP UserId was denied and only the authenticated normal account
was disabled. Fixtures and normal enforcement settings were restored afterward.

## Compatibility Envelope

Actual media bytes and Range responses were verified with X-Emby-Token,
X-Emby-Authorization, Authorization, api_key and URL X-Emby-Token. User-Agent
variants do not change entitlement. Encoded authorization metadata and stale
UserId handling were also checked at the Bot authentication boundary.

On this Emby version, a standalone URL `token` or URL authorization metadata
without another source-supported credential returns 401 both directly and
through Caddy. It is not classified as a proxy regression.

The positive suite compares exact video/audio/image/subtitle bytes and Range
headers between the source and gateway, both with and without /emby. It follows
Emby's PlaybackInfo-generated HLS manifests through child manifests and actual
transcoded TS segments, comparing segment hashes. The test verifies that the
VIP entitlement, expiry and IsDisabled state do not change. Separate negative
checks reject non-VIP access to the same real media and HLS segments.

Every app/device, every codec/container, live television, the real CDN and
Telegram delivery were not tested. No client-name allowlist was introduced.
Production configuration and production databases were not used. HLS continuity
is held in one Bot process; restart or a one-hour idle interval requires the
client to reopen playback and authenticate its manifest again.

## Reproduction And Evidence

- Run `python -B scripts/run_offline_tests.py` for the isolated suite.
- Opt-in scripts: `test_mysql_integration.py`, `test_emby_integration.py`,
  `test_vip_line_integration.py`, `test_vip_positive_media.py`, and
  `test_proxy_templates.py` under `scripts/`. The positive HLS test uses
  `requirements-test.txt`; it does not add a production dependency.
- Target evidence: `/opt/tgbot-validation/`, including `build-final.log`,
  `offline-final.log`, `mysql-integration.log`, `emby-integration.log`,
  `register-integration.log`, `vip-matrix-final-image.log`, and `vip-positive.log`.
- Test configuration and credentials remain restricted to the test host; they
  are not included here. The release is published only after the candidate
  passes validation; no production deployment is performed by this test run.

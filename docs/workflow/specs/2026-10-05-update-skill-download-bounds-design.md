# update-skill download bounds (#45)

## Problem

`update_skill/cmd.py` `_download` reads the whole response with no byte limit and lets urllib
follow redirects to any host. The text is written into agent instruction files.

## Scope

Change only `_download` and its tests. Open the URL through an opener built from an HTTPS handler
(existing SSL context) and a redirect handler. Its `http_error_30x` validates the `Location`
(or `URI`) header, joined against the request URL, before delegating to the stdlib, whose own
scheme pre-check would otherwise echo the target in an `HTTPError`. It raises `CommandError`
unless the target has scheme `https`, hostname `raw.githubusercontent.com` (parsed from
`_RAW_BASE`), and no explicit port. Comparison is exact on `urlsplit(...).hostname`, so `…githubusercontent.com.evil`,
`user@` tricks and `http://` fail. After opening, apply the same check to `resp.geturl()`. Read
at most `_MAX_DOWNLOAD_BYTES + 1` bytes (256 KiB; largest doc today is ~14 KB) and raise
`CommandError` above the limit, or when `resp.length` shows the body ended short of its declared
length (a bounded `read` does not raise `IncompleteRead`). Error text names the file, the expected host or limit, and a fix;
it never echoes the redirect target. Excluded: checksums/signatures, proxy and certificate handling.

### Failure model

- Actors and deployments: a local operator running `desloppify update-skill`; the server and any
  network party between it and GitHub.
- Invariants: instruction files only receive text served from the expected host, bounded in size.
- Accepted: content served by the expected host itself (ADR 0011 trust); a same-host redirect
  (GitHub serves from one host); slow-drip responses (15 s socket timeout bounds each read).
- Covered elsewhere: integrity of `main` (ADR 0011); TLS verification (`_ssl_context`).

### Threat model

- Boundaries: HTTP redirect `Location` (widened: now validated); response body (bounded).
- Actor: a party able to influence the response (compromised CDN path, misconfigured redirect).
  Trust stays with the GitHub host named in `_RAW_BASE`.
- Controls: exact scheme/host/port match before following; byte cap before decode. Failures
  raise `CommandError` naming file and expected host only.
- Out of scope: content from the expected host; proxy-level interception.

## Success

1. A body of limit+1 bytes raises `CommandError`; a body of exactly the limit is returned.
2. A redirect to another host, a lookalike host, a userinfo host, `http`, or an explicit port
   raises `CommandError`; a same-host `https` redirect is followed.
3. A final `geturl()` on another host raises `CommandError`.
4. Messages name the operation (download), the file, and a fix, and omit the redirect target;
   this holds for `file:`/`data:` targets too.
5. A body shorter than its declared `Content-Length` raises `CommandError`.

## Validation

- Criterion 1: focused-test — fake response in `test_update_skill_cmd_direct.py`, limit and
  limit+1 bytes.
- Criterion 2: focused-test — table over targets through the handler's `http_error_302`;
  plus one test driving `_download` through a fake HTTPS handler returning a 302.
- Criterion 3: focused-test — fake opener whose response reports a foreign `geturl()`.
- Criterion 4: focused-test — assert operation, file and fix present, target absent.
- Criterion 5: focused-test — fake response with remaining `length`.
- Download URL contract (ADR 0011): focused-test — existing fetch test, updated to the opener seam.

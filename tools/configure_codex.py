#!/usr/bin/env python3
"""Write (or restore) the ~/.codex/config.toml entries that put the relay in
the traffic path — so nobody has to edit TOML by hand.

    python tools/configure_codex.py --mode direct  --port 8787
    python tools/configure_codex.py --mode gateway --port 8788 \
        --api-key sk-...                     # key stored in config.toml
    python tools/configure_codex.py --mode gateway --port 8788 \
        --base-url http://relay.corp:8788/v1 # central relay, no local relay
    python tools/configure_codex.py --restore

What it does
------------
* backs the original up once, to config.toml.bak-codex-overload-fix
  (an existing backup is never overwritten, so it is always the pristine
  pre-install state);
* sets the top-level `model_provider`;
* replaces (idempotently) the matching [model_providers.*] block;
* `--restore` puts the backup back.

Auth in gateway mode: with `--api-key` the key is written into the block as a
static Authorization header. One file, works for the CLI *and* the desktop
app, survives reboots — the simplest thing that works for non-technical
users. Without `--api-key` the block uses `env_key` and the caller sets the
environment variable itself (better for shared machines; note GUI-launched
apps do not read shell profiles).

Standard library only. Exits non-zero on any problem without writing.
"""

import argparse
import difflib
import os
import re
import shutil
import stat
import sys

DIRECT_PROVIDER = "openai-retry"
GATEWAY_PROVIDER = "gateway-retry"
BACKUP_SUFFIX = ".bak-codex-overload-fix"


def _provider_block(mode, base_url, api_key=None, api_key_env="SUB2API_API_KEY"):
    lines = []
    if mode == "gateway":
        lines += [
            f"[model_providers.{GATEWAY_PROVIDER}]",
            'name = "Gateway (retry)"',
            f'base_url = "{base_url}"',
            'wire_api = "responses"',
        ]
        if api_key:
            # A static header: works for CLI and GUI-launched desktop apps
            # alike, no environment plumbing needed.
            lines.append(
                'http_headers = { Authorization = "Bearer %s" }' % api_key.replace('"', '\\"'))
        else:
            lines.append(f'env_key = "{api_key_env}"')
        lines += [
            "stream_max_retries = 20",
            "request_max_retries = 10",
        ]
    else:
        lines += [
            f"[model_providers.{DIRECT_PROVIDER}]",
            'name = "OpenAI (retry)"',
            f'base_url = "{base_url}"',
            'wire_api = "responses"',
            "requires_openai_auth = true",
            "stream_max_retries = 20",
            "request_max_retries = 10",
        ]
    return "\n".join(lines) + "\n"


def _split_tables(text):
    """Yield (start, end) line ranges of each [table] block, header included."""
    lines = text.split("\n")
    ranges = []
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            if start is not None:
                ranges.append((start, i))
            start = i
    if start is not None:
        ranges.append((start, len(lines)))
    return lines, ranges


def _remove_provider_blocks(text, names):
    """Delete every [model_providers.<name>] block for name in names."""
    lines, ranges = _split_tables(text)
    skip = set()
    for start, end in ranges:
        header = lines[start].strip()
        m = re.match(r'^\[model_providers\.([^"\]]+)\]$', header)
        if m and m.group(1) in names:
            skip.update(range(start, end))
    out = [lines[i] for i in range(len(lines)) if i not in skip]
    text = "\n".join(out)
    # collapse the blank lines the removal may have doubled up
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _set_model_provider(text, provider):
    """Set the top-level model_provider, wherever it currently sits."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            break  # top-level region ends at the first table header
        if re.match(r"^\s*model_provider\s*=", line):
            lines[i] = f'model_provider = "{provider}"'
            return "\n".join(lines)
    # not present before the first table header -> prepend at the very top
    return f'model_provider = "{provider}"\n' + "\n".join(lines)


def write_config(path, mode, base_url, api_key=None, api_key_env="SUB2API_API_KEY",
                 dry_run=False):
    backup = path + BACKUP_SUFFIX
    original = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            original = fh.read()

    provider = GATEWAY_PROVIDER if mode == "gateway" else DIRECT_PROVIDER
    text = _remove_provider_blocks(original, {DIRECT_PROVIDER, GATEWAY_PROVIDER})
    text = _set_model_provider(text, provider)
    block = _provider_block(mode, base_url, api_key, api_key_env)
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n" + block

    diff = "\n".join(difflib.unified_diff(
        original.splitlines(), text.splitlines(),
        fromfile="config.toml (before)", tofile="config.toml (after)", lineterm=""))

    if dry_run:
        print(diff)
        return 0

    if not os.path.isdir(os.path.dirname(path) or "."):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    if original and not os.path.exists(backup):
        shutil.copy2(path, backup)
        print(f"backup    {backup}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if api_key and os.name == "posix":
        # the config now holds a bearer key; keep it owner-only
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    print(diff)
    print(f"wrote     {path}")
    return 0


def restore_config(path, dry_run=False):
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        print(f"no backup at {backup} — nothing to restore")
        return 1
    if dry_run:
        print(f"would copy {backup} -> {path}")
        return 0
    shutil.copy2(backup, path)
    print(f"restored  {path} from {backup}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Write or restore the Codex config.toml entries for the relay.")
    ap.add_argument("--config", default=os.path.join(
        os.path.expanduser("~"), ".codex", "config.toml"))
    ap.add_argument("--mode", choices=["direct", "gateway"])
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--base-url", default=None,
                    help="default http://127.0.0.1:<port> (direct) or .../v1 (gateway); "
                         "pass explicitly for a central company relay")
    ap.add_argument("--api-key", default=None,
                    help="gateway API key; written into config.toml as a static "
                         "Authorization header (simplest for non-technical users)")
    ap.add_argument("--api-key-env", default="SUB2API_API_KEY",
                    help="env var name when --api-key is not used (gateway mode)")
    ap.add_argument("--restore", action="store_true",
                    help="restore config.toml from the pre-install backup")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the diff, change nothing")
    args = ap.parse_args()

    if args.restore:
        return restore_config(args.config, args.dry_run)
    if not args.mode:
        ap.error("--mode is required (or use --restore)")
    if args.base_url is None:
        if args.port is None:
            args.port = 8788 if args.mode == "gateway" else 8787
        args.base_url = f"http://127.0.0.1:{args.port}"
        if args.mode == "gateway":
            args.base_url += "/v1"
    return write_config(args.config, args.mode, args.base_url,
                        args.api_key, args.api_key_env, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

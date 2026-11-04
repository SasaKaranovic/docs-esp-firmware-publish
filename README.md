# docs-esp-firmware-publish

GitHub Action that publishes ESP32 firmware to the web flasher on
[docs.sasakaranovic.com/firmware](https://docs.sasakaranovic.com/firmware/).

It uploads each build to the product's folder on the docs server and adds it to that
product's `builds.json`. The product page picks it up immediately; the docs site doesn't
need to be rebuilt.

```
/firmware/<product>/builds.json
/firmware/<product>/builds/<variant>-<version>/manifest.json
/firmware/<product>/builds/<variant>-<version>/firmware.bin
```

Products, variants and channels are defined in `firmware.yml` in the docs repo. Before
uploading, the action reads `product.json` (written there by the docs build) from the
target folder on the server and refuses to publish if it belongs to a different product, or
if the variant or channel isn't defined. It never deletes builds.

## Usage

```yaml
concurrency:                       # one publish per product at a time
  group: firmware-publish-openfan-micro
  cancel-in-progress: false

# … build steps that produce a merged image (bootloader + partitions + app, flashed at 0x0) …

- uses: SasaKaranovic/docs-esp-firmware-publish@v1
  with:
    product: openfan-micro
    builds: |
      standard=build/standard/firmware.bin
      unraid=build/unraid/firmware.bin
    host: ${{ secrets.FIRMWARE_DEPLOY_HOST }}
    ssh-key: ${{ secrets.FIRMWARE_DEPLOY_KEY }}
    known-hosts: ${{ secrets.FIRMWARE_KNOWN_HOSTS }}
```

Complete workflows for each build system are in [`examples/`](examples/):
[PlatformIO](examples/platformio.yml), [ESP-IDF](examples/esp-idf.yml),
[Arduino CLI](examples/arduino-cli.yml) and [ESPHome](examples/esphome.yml).

### Defaults

| Input | Default |
| --- | --- |
| `channel` | Tag push or release → `stable`; default branch → `preview`; any other branch → `dev` (only shown on the flasher with `?dev`) |
| `version` | Tag name without a leading `v` (`v1.4.0` → `1.4.0`); otherwise `YYYY.MM.DD-<short sha>` |
| `notes` | Release body for `release` events, otherwise the head commit message |
| `date` | Now (UTC) |

The build id is `<variant>-<version>`. Re-running a workflow for the same commit replaces
that build (rebuilds are rarely byte-identical). Publishing a different commit or file under
an existing id fails unless `replace: true` is set.

### All inputs

| Input | Required | Description |
| --- | --- | --- |
| `product` | yes | Product key from `firmware.yml` |
| `builds` | yes | One `variant=path` per line; each path is a merged image flashed at offset 0 |
| `host` | yes | SSH host of the web server. Use the server's IP or a DNS-only name: SSH doesn't pass through the Cloudflare proxy |
| `ssh-key` | yes | Private key for the `docs-firmware` user (secret) |
| `known-hosts` | yes | `known_hosts` line(s) for the server, from `ssh-keyscan` (secret) |
| `channel`, `version`, `notes`, `date` | no | See defaults above |
| `port` | no | Default `22` |
| `user` | no | Default `docs-firmware` |
| `remote-path` | no | Empty for per-product keys. With an admin key locked to `/firmware/`, use `<product>/` |
| `replace` | no | Overwrite a build with the same id. Default `false` |
| `erase` | no | Ask the user to erase the device before installing. Default `false` |
| `record-source` | no | Store repo, commit and run links in `builds.json`. Default `true` |
| `base-url` | no | Used for links in the run summary. Default `https://docs.sasakaranovic.com` |
| `dry-run` | no | Validate and print the rsync commands without uploading |

Outputs: `build-ids`, `version`, `channel`, `flasher-url`. A summary with flasher links is
added to the workflow run.

## Secrets per product repo

| Secret | Value |
| --- | --- |
| `FIRMWARE_DEPLOY_HOST` | Server IP or DNS-only hostname |
| `FIRMWARE_DEPLOY_KEY` | Private key of this repo's deploy key (locked to its product folder on the server) |
| `FIRMWARE_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519 <host>` |

Server setup and key creation are documented in `server-setup.md` in the docs repo.

## Running outside GitHub Actions

`publish.py` uses only the Python standard library plus `rsync` and `ssh` (Linux or macOS):

```bash
python3 publish.py --product openfan-micro \
  --build standard=firmware.bin --channel stable --version 1.4.0 \
  --target docs-firmware@203.0.113.10: --ssh-key ~/.ssh/fw_openfan_micro \
  --known-hosts ./known_hosts --dry-run
```

## Concurrency

Every publish re-reads `builds.json` right before uploading it and retries if it changed,
but the workflow should still use a `concurrency` group per product so two runs don't
upload at the same moment. Note that GitHub keeps only one *queued* run per group: if
several pushes queue up, older queued runs are cancelled and only the newest publishes.

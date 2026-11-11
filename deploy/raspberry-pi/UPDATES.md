# Remote player releases

The platform operator publishes one global desired release. All enrolled devices
(including registered, not-yet-paired devices) receive it on their next successful
authenticated HTTPS configuration poll. Offline devices receive the latest release
when they reconnect; intermediate releases are skipped. There is no account-admin
approval, scheduling, staged rollout, inbound SSH, or separate admin application.
Publication is an operator-only management command, not a dashboard permission.

## Start here: what you do, and how often

This walkthrough uses your existing Azure Container Apps deployment, a separate
Azure Key Vault for signing keys, and your Windows administrator workstation.
You do not need Azure CLI, Azure credentials, or the private key on a Pi.

| Task | How often |
| --- | --- |
| Create the signing vault and key pair (section 1) | Once for your installation |
| Install the updater and public key on a Pi (section 2) | Once per Pi |
| Build, sign, and publish a release (section 3) | Each player release |
| Run `azd deploy` | Each server release; this does not publish player code |

**Public key** means the file a Pi uses to recognize your releases. It is safe
to copy. **Private key** means the file that authorizes new code to run on every
Pi. Never copy it to a Pi, the web server, Git, an artifact container, or chat.

The Azure templates do not create the signing vault or a publishing job.
The manual publishing procedure below uses a shell in the running Azure app,
so it automatically uses the production database, storage, and managed identity.
It gives that app **no access to your signing vault**.

## 1. One time: create and store your signing keys in Azure

### 1.1 Prepare your Windows workstation

Use a trusted Windows workstation with PowerShell **7.4 or newer**, Azure CLI,
OpenSSL with Ed25519 support, and Python 3.10+. Use the existing repository
checkout and its `.venv`. Keep private files outside the checkout and outside
cloud-synced folders.

Run the Windows commands below in the **same PowerShell terminal**, starting at
the repository root. Replace the example values with your existing Azure values:

```powershell
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$SubscriptionId = "YOUR-SUBSCRIPTION-ID"
$ResourceGroup = "rg-pisign"
$AppName = "pisign"
$VaultName = "YOUR-GLOBALLY-UNIQUE-SIGNING-VAULT"
$Work = Join-Path $env:LOCALAPPDATA "PiSignReleases"

az login
az account set --subscription $SubscriptionId
New-Item -ItemType Directory -Force -Path $Work | Out-Null
openssl version
.\.venv\Scripts\python.exe --version
```

Find the actual app/resource-group names in Azure Portal > **Container Apps**;
do not assume the examples match your environment. Choose a new vault name
(3-24 letters, numbers, and hyphens, starting with a letter).

### 1.2 Create a separate signing vault

Do **not** reuse the application's existing vault: its managed identity already
has access to that vault. Your signing vault should be accessible only to trusted
release administrators or a separately authorized signing identity.

```powershell
$Location = az group show --name $ResourceGroup --query location -o tsv
$VaultId = az keyvault create --name $VaultName --resource-group $ResourceGroup `
    --location $Location --enable-rbac-authorization true `
    --enable-purge-protection true --query id -o tsv
$MyObjectId = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee-object-id $MyObjectId `
    --assignee-principal-type User --role "Key Vault Secrets Officer" `
    --scope $VaultId --output none
```

Creating the role assignment requires appropriate RBAC permissions, such as
Owner or Role Based Access Control Administrator at the relevant scope.
If denied, have your Azure administrator create this assignment. Allow a few
minutes for propagation before the next step; a `Forbidden` response is not a
reason to grant the web app access. Follow your organization's vault networking
and audit-logging requirements.

### 1.3 Generate the key pair and upload it

Use Key Vault **Secrets**, not **Keys**. Azure Key Vault's native key types do not
support this updater's Ed25519 signatures. Storing the PEM as a secret protects
it at rest, but signing still temporarily downloads it to your workstation;
this is not non-exportable HSM signing.

The following creates a restricted temporary folder, uploads the private key,
and removes the local private copy even if a command fails. Do not rerun this
step to publish another release: that would generate a different signing key.

```powershell
$KeyDir = Join-Path $env:TEMP ("pisign-signing-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $KeyDir | Out-Null
$Me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $KeyDir /inheritance:r /grant:r "${Me}:(OI)(CI)F" | Out-Null
$PrivateFile = Join-Path $KeyDir "release-private.pem"
$PublicFile = Join-Path $Work "release-public.pem"

try {
    openssl genpkey -algorithm Ed25519 -out $PrivateFile
    openssl pkey -in $PrivateFile -pubout -out $PublicFile
    $PrivateSecretId = az keyvault secret set --vault-name $VaultName `
        --name player-release-private --file $PrivateFile --encoding utf-8 `
        --query id -o tsv
    $PublicSecretId = az keyvault secret set --vault-name $VaultName `
        --name player-release-public --file $PublicFile --encoding utf-8 `
        --query id -o tsv
} finally {
    if (Test-Path $PrivateFile) { Remove-Item -LiteralPath $PrivateFile }
    Remove-Item -LiteralPath $KeyDir
}

$PublicFingerprint = (Get-FileHash -LiteralPath $PublicFile -Algorithm SHA256).Hash.ToLowerInvariant()
"Private secret ID: $PrivateSecretId"
"Public secret ID:  $PublicSecretId"
"Public SHA-256:    $PublicFingerprint"
```

Record these **two versioned secret IDs and the public fingerprint** in your
administrator runbook/password manager. These identifiers and the public
fingerprint are not private-key material. The IDs end with a version; keep that
version rather than silently switching to the newest secret.

This guide fingerprints the exact bytes of the PEM file, not its DER
representation. Copy/download the file without opening and resaving it in an
editor; changing line endings changes this fingerprint.

If either upload failed, stop and inspect the vault before generating anything
else. Do not provision devices until you have a matching, successfully stored
private/public pair. Never delete or replace the only copy of the private key.
Use Key Vault's soft-delete/purge protection and your organization's approved
backup/recovery procedure.

## 2. Once per Pi: copy the public key and install the updater

### 2.1 Retrieve the approved public key on Windows

If using a new terminal, repeat section 1.1 and set the following ID to the
**public** secret ID recorded in section 1.3. You do not need the private key.

```powershell
$PublicSecretId = "https://YOUR-VAULT.vault.azure.net/secrets/player-release-public/YOUR-VERSION"
$PublicFile = Join-Path $Work "release-public.pem"
if (Test-Path $PublicFile) { Remove-Item -LiteralPath $PublicFile }
az keyvault secret download --id $PublicSecretId --file $PublicFile --encoding utf-8
(Get-FileHash -LiteralPath $PublicFile -Algorithm SHA256).Hash.ToLowerInvariant()
```

Compare this result to your recorded fingerprint. Stop if it differs.
Copy `release-public.pem` onto a USB drive you control, connect it to the Pi,
and copy it from the file manager into the desktop user's home directory.
Alternatively, use SCP over SSH after verifying the Pi's SSH host key.
**Do not copy the private key.**

On the Pi, open a terminal as its normal desktop user:

```sh
sha256sum "$HOME/release-public.pem"
```

Compare the 64-character hash with the approved fingerprint displayed on your
Windows workstation. Stop if it differs. This authenticated Azure download,
controlled transfer, and comparison is the "trusted provisioning channel";
you do not need to configure an Azure connection on the Pi.

### 2.2 Install the updater on the Pi

Use Raspberry Pi OS Bookworm or newer, OS Python 3.10+, `python3-venv`,
OpenSSL with Ed25519 support, and space for three player environments.
The graphical session and Chromium must already work.

**Stop existing playback first**, including its automatic launcher for this
session. Run the following as the existing desktop user, not root. Replace the
server URL. Preserve any existing `--state-dir`, `--name`, `--browser`, and
`--software-rendering` options by adding them to `install-updater` below.

```sh
cd "$HOME/pysign"
git pull --ff-only
.venv/bin/python -m pip install -e '.[player]'
.venv/bin/python -m player install-updater \
  --server https://signage.example.com \
  --public-key "$HOME/release-public.pem"
```

Bootstrap needs network access to install the pinned dependencies. It copies a
fallback player and the public key, creates its own virtualenv, and installs
the independent supervisor under `~/.local/share/pisign-updater`.
It replaces the standard desktop/labwc launchers with the supervisor launcher.
If you use a custom X11 launcher, change its player command to:

```sh
/usr/bin/python3 "$HOME/.local/share/pisign-updater/updater.py" \
  --home "$HOME/.local/share/pisign-updater"
```

Keep only one launcher enabled, then reboot:

```sh
sudo reboot
```

After the next successful configuration poll, check the display's dashboard
settings: its installed version should be `bootstrap`, or the published release
version if it has already updated. If it is unknown, check that the supervisor,
not the old checkout, is launching. Bootstrap refuses to overwrite an existing
updater installation; do not delete that installation to retry casually.

A successful bootstrap no longer depends on the checkout or its virtualenv.
Do not use live `git pull` to update a supervised player. Devices without
bootstrap continue playback and remote restart, but cannot install releases
or report their version.

## 3. Each player release: build, sign, and publish

### 3.1 Deploy the server and choose the release target

On Windows, at the repository root, deploy the server normally:

```powershell
azd deploy
```

The existing deployment hook applies migrations before rolling out the web app.
This assumes private asset storage and its managed-identity role assignment
are already provisioned; see the [Azure deployment guide](../README.md) if not.
You do not need to add the private key, or a permanent public-key mount, to the
web app. This walkthrough supplies the public key only to the publishing command.

On each Pi/OS combination you intend to support, obtain the machine identifier
and **OS Python** minor version:

```sh
/usr/bin/python3 -c 'import platform, sys; print(platform.machine()); print(f"{sys.version_info.major}.{sys.version_info.minor}")'
```

Use only combinations actually tested with the release. The example below targets
`aarch64` and Python `3.13`; **replace those values with your tested targets**.
Listing several machines and Python versions advertises every combination.
Chromium and Python are not bundled. Compatibility also requires updater
protocol 1 and state schema 1.

### 3.2 Build and sign on Windows

If using a new terminal, repeat section 1.1, then restore both versioned secret
IDs from your runbook. Retrieve/check the public key using section 2.1.
Choose a unique semantic version and a positive integer sequence **higher than
every previously published sequence**. For the first release, use `1.0.0` and `1`;
for the next, for example, `1.0.1` and `2`.

```powershell
$PrivateSecretId = "https://YOUR-VAULT.vault.azure.net/secrets/player-release-private/YOUR-VERSION"
$Sequence = 1
$Version = "1.0.0"
$ReleaseDir = Join-Path $Work "release-$Sequence"
$KeyDir = Join-Path $env:TEMP ("pisign-signing-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $KeyDir | Out-Null
$Me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $KeyDir /inheritance:r /grant:r "${Me}:(OI)(CI)F" | Out-Null
$PrivateFile = Join-Path $KeyDir "release-private.pem"

try {
    az keyvault secret download --id $PrivateSecretId --file $PrivateFile --encoding utf-8
    .\.venv\Scripts\python.exe -m tools.build_player_release `
        --version $Version --sequence $Sequence `
        --machines aarch64 --python 3.13 `
        --private-key $PrivateFile --output $ReleaseDir
} finally {
    if (Test-Path $PrivateFile) { Remove-Item -LiteralPath $PrivateFile }
    Remove-Item -LiteralPath $KeyDir
}
```

The builder downloads pinned pure-Python wheels for `websocket-client` and
`segno`, packages player code/resources and wheel hashes, and signs the manifest.
It creates `player-1.json` and `player-1.tar.gz` for sequence 1.
The private key is not included. The player installs dependencies offline with
`--require-hashes --no-index`. Existing output files are not overwritten.

Test the signed artifact on your supported Pi/OS combinations before production
publication, using a separate test server and test device with the same trusted
public key. Include cached offline playback and failed-start rollback.
**Production publication targets every enrolled device immediately**; there is
no per-device production test rollout.

### 3.3 Upload three non-secret files to private Azure storage

In Azure Portal, open your app's **Containers > Environment variables** and find
`PISIGN_ASSET_AZURE_ACCOUNT_URL` and `PISIGN_ASSET_AZURE_CONTAINER`.
Use the storage account name from the URL (the part before `.blob.core.windows.net`)
and the container name below. Do not use a public container or enable anonymous
access. Your Azure user needs **Storage Blob Data Contributor** on that container;
ask your administrator if upload is denied. Your workstation also needs network
access to its Blob endpoint; use your approved VPN/private network if required.

```powershell
$StorageAccount = "YOUR-EXISTING-ASSET-STORAGE-ACCOUNT"
$AssetContainer = "assets"
$PublicFingerprint = (Get-FileHash -LiteralPath $PublicFile -Algorithm SHA256).Hash.ToLowerInvariant()
$StagingPrefix = "player-release-staging/$Sequence"

az storage blob upload --account-name $StorageAccount --container-name $AssetContainer `
    --auth-mode login --name "$StagingPrefix/release-public.pem" `
    --file $PublicFile --overwrite false --output none
az storage blob upload --account-name $StorageAccount --container-name $AssetContainer `
    --auth-mode login --name "$StagingPrefix/player-$Sequence.json" `
    --file (Join-Path $ReleaseDir "player-$Sequence.json") --overwrite false --output none
az storage blob upload --account-name $StorageAccount --container-name $AssetContainer `
    --auth-mode login --name "$StagingPrefix/player-$Sequence.tar.gz" `
    --file (Join-Path $ReleaseDir "player-$Sequence.tar.gz") --overwrite false --output none

"Sequence: $Sequence"
"Public SHA-256: $PublicFingerprint"
```

If a staging name already exists, stop and inspect it rather than overwriting
it blindly. These are temporary transport copies, not published releases.
Do not upload anything from the temporary private-key folder.

### 3.4 Publish inside the running Azure app

Do this **once**, with no other publication or deployment in progress.
Open an interactive shell from your Windows terminal:

```powershell
az containerapp exec --resource-group $ResourceGroup --name $AppName --command /bin/sh
```

The following commands now run **in the Linux container**, not PowerShell.
Replace `1` with your sequence and `REPLACE_WITH_APPROVED_SHA256` with the
64-character approved public fingerprint from your Windows workstation/runbook.
Do not obtain the expected fingerprint from the staging blobs themselves.

```sh
cd /app
export PISIGN_RELEASE_SEQUENCE=1
export PISIGN_RELEASE_PUBLIC_SHA256=REPLACE_WITH_APPROVED_SHA256
python - <<'PY'
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient

sequence = int(os.environ["PISIGN_RELEASE_SEQUENCE"])
expected = os.environ["PISIGN_RELEASE_PUBLIC_SHA256"].lower()
if sequence < 1 or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
    raise ValueError("Set a positive sequence and the approved 64-character SHA-256.")

with ManagedIdentityCredential(
    client_id=os.environ["PISIGN_ASSET_AZURE_CLIENT_ID"]
) as credential, BlobServiceClient(
    account_url=os.environ["PISIGN_ASSET_AZURE_ACCOUNT_URL"],
    credential=credential,
) as service, tempfile.TemporaryDirectory(prefix="pisign-publish-") as directory:
    root = Path(directory)
    container = service.get_container_client(os.environ["PISIGN_ASSET_AZURE_CONTAINER"])
    names = ["release-public.pem", f"player-{sequence}.json", f"player-{sequence}.tar.gz"]
    for name in names:
        with (root / name).open("wb") as target:
            container.download_blob(f"player-release-staging/{sequence}/{name}").readinto(target)
    public_key = root / "release-public.pem"
    if hashlib.sha256(public_key.read_bytes()).hexdigest() != expected:
        raise ValueError("Public-key fingerprint mismatch; nothing was published.")
    os.environ["PISIGN_PLAYER_RELEASE_PUBLIC_KEY"] = str(public_key)
    subprocess.run(
        ["python", "manage.py", "publish_player_release",
         str(root / f"player-{sequence}.json"), str(root / f"player-{sequence}.tar.gz")],
        check=True,
    )
PY
```

Expect `Published 1.0.0 (sequence 1) to all enrolled devices.` for the first
release. Exceptions or a missing success message are **not success**: inspect
the error before retrying. Publication verifies the signature, archive digest
and size, and increasing sequence, then stores an independent immutable copy in
the production private asset store and records it in the production database.
Only the publishing subprocess receives the temporary public-key path; no
permanent container environment setting or filesystem change is required.

Type `exit` to leave the container shell and return to PowerShell.

### 3.5 Verify delivery and remove the transport copies

Online devices discover the release on the next successful configuration poll
(normally every 30 seconds), then stage and health-check it. In display settings,
check the installed version, target, update status, error, and report timestamp.
Allow time for download/install and the 30-second startup health window; a poll
alone does not mean installation completed. Offline devices catch up later.

After confirmed publication, remove **only these three temporary staging blobs**:

```powershell
foreach ($Name in @("release-public.pem", "player-$Sequence.json", "player-$Sequence.tar.gz")) {
    az storage blob delete --account-name $StorageAccount --container-name $AssetContainer `
        --auth-mode login --name "$StagingPrefix/$Name" --output none
}
```

Do not delete the separately stored published artifacts: offline players may
still need them. Keep the signed build outputs and release version/sequence in
your release archive. For the next release, repeat section 3 with a higher
sequence and new version; reuse the same signing key.

Key rotation and supervisor upgrades require trusted local reprovisioning.
Creating a new Key Vault secret version does **not** update keys already on Pis.
Do not rotate the signing key as part of routine release publication.

## Activation, health, and recovery

The existing player poll sends installed version, target, status and error,
and writes the desired signed release to a mailbox outside its cached playlist.
The supervisor independently verifies the signature, compatibility and anti-replay
sequence. It downloads with the server-bound bearer credential, rejects redirects,
checks the exact signed digest/size, rejects unsafe archives, and creates a fresh
virtualenv while the installed player keeps running.

Only after staging succeeds does it stop the old player and Chromium process group
and launch the candidate. A per-launch, fresh heartbeat must show a responsive
playback loop and live Chromium for 30 continuous seconds within 120 seconds.
This is a startup check, not proof every external website works. It also works
with a cached playlist or pairing screen without internet access after download.
On failed startup, the supervisor restores the previous environment automatically.
An interrupted activation rolls back on the next supervisor start; its durable
journal is written before activation. A successfully installed release becomes the
next fallback. No root privileges or reboot are required for normal updates.

Bad signatures never advance the anti-replay floor. Incompatible, invalid,
dependency-install and startup-failing releases are recorded
and not repeatedly attempted, even after reboot. Publish a corrected artifact
with a **new sequence and version**; do not mutate a published release. Transient
network download failures retry after 60 seconds while playback continues.
Staging interrupted by shutdown before activation can also retry; an interrupted
candidate startup rolls back and is not retried.
To revert a fleet intentionally, publish the previous compatible player code as
a new higher sequence/version, rather than replaying an old manifest.

Pairing credentials, playlists, cached assets and Chromium profiles remain in
`~/.local/state/pisign`; activation never resets or migrates them. **Schema 1
releases must read and write state compatible with both the preceding release
and the bootstrap fallback.** Do not perform destructive migrations, browser
profile upgrades, or irreversible server API changes in a player release.
A state-schema change needs a separately designed migration/recovery protocol,
not a changed manifest value.

The dashboard reports the installed version, last update state, target and error
with its last-report time. Offline reports can be stale; refresh after a poll.
Supervisor output goes to `player.log` with the standard labwc launcher; desktop
session output depends on the OS session logger. The updater's `record.json`
retains failed sequences and the current/fallback journal. Release environments
are retained for diagnosis; monitor disk space and remove only unused environments
locally while the supervisor is stopped, never its current or pending release.
Normal remote restart still restarts the player and Chromium, not the supervisor
or the OS. Remote reset remains explicitly destructive and unrelated to updates.

## Explicit installation boundaries

Remote releases update **unprivileged player code and its Python dependencies
only**. The independent supervisor, trust key, OS, Python, Chromium, desktop
configuration and system packages are separately maintained.

The SD-card Wi-Fi service executes the root-owned copy at
`/usr/local/lib/pisign/wifi.py`, installed by `install-wifi.sh`. A release may carry
the player's Wi-Fi status constants/module, but it does **not** replace that
privileged copy, service unit, or NetworkManager settings. Updating the privileged
importer requires a separate trusted local `sudo sh deploy/raspberry-pi/install-wifi.sh`
installation. Never point a root service at a user-writable release or virtualenv.

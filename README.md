# fdmedia.studio client gallery

The gallery backend, media worker, and album-management command are Python. The
browser gallery remains JavaScript because browsers do not run ordinary Python;
the Python service serves its static files and PhotoSwipe assets.

## Publish the Docker image to GitHub

The GitHub Actions workflow in `.github/workflows/publish-image.yml` builds and
publishes the image to GitHub Container Registry (GHCR) when code is pushed.
Push this project to a GitHub repository, then open **Actions** and confirm the
`Publish TrueNAS image` workflow succeeds. The image name is
`ghcr.io/<owner>/<repository>`; use lowercase letters for the owner and
repository in the image name.

GHCR packages are private by default. On GitHub, open the repository's
**Packages** page, select the image package, then use **Package settings** to
change its visibility to **Public**. TrueNAS can then pull it without registry
credentials. The default branch publishes the `latest` tag; pushing a version
tag such as `v1.0.0` also publishes that version tag.

## What you need

- A TrueNAS SCALE server and a storage pool/dataset for photos, app data, and cache.
- A public GitHub Container Registry image for this project.
- TrueNAS SCALE 24.10 or later.
- A DNS hostname and HTTPS reverse proxy or Cloudflare Tunnel for public access.

## 1. Prepare storage and permissions on TrueNAS

You can use folders inside an existing dataset; separate datasets are optional.
Separate datasets are useful if you want independent ACLs, snapshots, quotas, or
replication. For a simple setup, create these folders inside an existing pool
dataset (or use the existing photo folder):

```text
/mnt/Apps/Site/clients/media      # source photos
/mnt/Apps/Site/apps/fdmedia-gallery/data    # albums and favorites
/mnt/Apps/Site/apps/fdmedia-gallery/cache   # generated thumbnails and video previews
```

These example paths assume the pool is named `Apps`. If your pool has a
different name, replace `/mnt/Apps` with the correct pool path. Ensure the
folders already exist before starting Compose. Set the app data and cache folders so
UID/GID `568:568` can read and write. The web service needs write permission
on the photo folder so an administrator can permanently delete originals; the
worker only needs read/traverse permission. Use the TrueNAS ACL editor and apply
permissions to child files and directories where appropriate.

Put each album's photos in a subfolder, such as
`/mnt/Apps/Site/clients/media/wedding-2026`.

## 2. Set a permanent application secret

The safest option is to generate the secret directly into `.env` on TrueNAS
(the file is created in step 3). This avoids printing it to the terminal. Do
not wrap `$(openssl ...)` in single quotes; that would write the text literally
instead of running OpenSSL.

If you need to generate a secret separately, run this in the TrueNAS shell:

```sh
openssl rand -hex 32
```

Copy the output and keep it in a password manager. If you prefer to generate it
on Windows instead, run this in PowerShell (not the TrueNAS shell):

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use exactly the same value for every web container replica (this setup uses
one). Do not use the sample value from any example. Changing the secret logs out
all gallery visitors. The admin page also needs its own password; add that
separately in step 3.

## 3. Install the image on TrueNAS

Set `IMAGE` to the package URL for your GitHub repository, for example
`ghcr.io/alexs/fdmedia-gallery:latest` (replace `alexs` and the repository name
with yours). You can install the two services either with the TrueNAS Apps YAML
editor or with Docker Compose over SSH.

For the Apps YAML editor, open **Apps > Discover Apps > Custom App** and paste
the contents of `docker-compose.yml`. Replace `${IMAGE:?...}` with your GHCR
image URL, and replace the `GALLERY_SECRET` and `ADMIN_PASSWORD` expressions
with their generated values from step 2. Check that the host paths match your
datasets before installing. Do not also deploy the same app with Docker Compose.

For a shell-based Compose deployment, copy only `docker-compose.yml` to a
folder on TrueNAS, then open the TrueNAS Shell or connect over SSH. Change into
that folder and confirm Docker Compose is available:

```sh
cd /path/to/fdmedia-gallery
docker compose version
```

Create a private `.env` next to the Compose file. Set `IMAGE` to your public
GHCR package URL and include separate secrets for app sessions and admin access:

```sh
umask 077
printf 'IMAGE=ghcr.io/OWNER/REPOSITORY:latest\nGALLERY_SECRET=%s\nADMIN_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env
```

Save both values securely. `GALLERY_SECRET` signs client login cookies;
`ADMIN_PASSWORD` is the password for the album management page. Replace
`OWNER/REPOSITORY` with the lowercase GitHub owner and repository name. If you
already have a `.env`, preserve the existing secret values.

Edit `docker-compose.yml` to confirm the media, data, and cache host paths match
your TrueNAS datasets. The web service's photo mount must be writable for
administrator photo deletion; the worker's photo mount remains read-only. Keep
both data/cache mounts writable by UID/GID `568:568`. Then pull and start the services:

```sh
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100 web worker
```

When using Compose, recreate the web service after changing its environment:

```sh
sudo docker compose up -d --force-recreate web
```

On TrueNAS, an administrator shell account may still lack permission to access
the Docker daemon socket. The `permission denied` error for
`/var/run/docker.sock` is a host privilege issue, not an app configuration
error; use `sudo` for Docker Compose commands as shown. Do not `chmod` the
Docker socket or make it world-writable. If `sudo` is unavailable or still
returns a socket permission error, stop and use the SCALE-supported Apps
workflow or ask the TrueNAS administrator to run the commands; do not change
host Docker socket ownership or permissions.

The web service serves the gallery; the worker checks albums every 30 seconds
and generates previews for newly uploaded or changed photos. Keep the event
gallery page open or refresh it; new photos are added automatically after the
worker finishes processing them. No service restart is needed for photo uploads.

If `docker compose version` fails, do not install Docker packages manually on
the TrueNAS host. Use the SCALE Apps workflow instead.

If pulling the image fails with a registry DNS timeout, check resolution from
the TrueNAS shell:

```sh
getent hosts ghcr.io
cat /etc/resolv.conf
```

If name lookup fails, fix the DNS server configured for TrueNAS in the SCALE
Network/Global Configuration settings. Use a DNS server reachable and permitted
on your network (often your router, or a DNS service approved for your network).
Then retry `getent hosts ghcr.io`; only after it returns an address, retry the
image pull:

```sh
sudo docker compose pull
sudo docker compose up -d
```

If host name lookup works but pulling the image still times out, check that
TrueNAS has outbound internet access to GitHub Container Registry and that the
network/firewall allows DNS and HTTPS.

## 4. Add an album and PIN

For the friendly browser-based workflow, open `https://YOUR_HOST/admin` and
enter `ADMIN_PASSWORD`. For private LAN-only HTTP access, first change
`COOKIE_SECURE` to `"false"` in the `web` service in `docker-compose.yml` and
recreate the web container; never use that setting when the admin page is
internet-facing:

```yaml
COOKIE_SECURE: "false"
```

Then visit `http://TRUE_NAS_LAN_IP:3000/admin`. Create an album by entering its
display title, URL slug, existing folder relative to `/media`, and the PIN
clients will use. Choose one of the built-in themes (default, wedding, baptism
boy/girl, or prom boy/girl) and optionally add a gallery description. Leaving
the description empty uses the default message, “Am păstrat aici momentele
voastre. Bucurați-vă de ele.” The page also lists existing albums and displays
their theme and description. Use the **Șterge** button to remove an album from
the app and disable its gallery link; this does not delete original photos from
`/media`.

The photo folder must already exist and be accessible inside `/media`. The web
service needs write permission to permanently delete original photos; the worker
continues to use a read-only mount. In an authenticated gallery, **Descarcă
galeria ZIP** downloads all original files in one archive. After administrator
login, each gallery item also has a **Șterge** button; confirming it permanently
deletes that original file from the media folder. Keep the admin password private
and use HTTPS when accessing the admin page outside your trusted LAN. The admin
login expires after 12 hours.

The easiest SMB workflow is to map two SMB shares in Windows: the app data
dataset (the directory that should contain `albums.json`) as `Y:`, and the
photos dataset as `Z:`. Put the event photos in a subfolder such as
`Z:\wedding-2026`. Run the included standard-library-only CLI on Windows;
the DATA and MEDIA paths point to those mapped shares, and the PIN is prompted
without being echoed:

```powershell
$env:DATA = 'Y:\'
$env:MEDIA = 'Z:\'
py .\cli.py add wedding-2026 "Wedding 2026" wedding-2026
```

If `py` isn't available, install Python 3 from python.org first. Keep the
project's `cli.py` on your computer; copying it onto the dataset is optional.
Run it from a local project copy with the two shares mapped. It writes
`Y:\albums.json`; inside the container, that same file is available as
`/data/albums.json`, and the album folder is found under `/media`. Confirm the
shares map to the roots of the datasets, not one directory above or below them.

Alternatively, after the app is deployed, create the album in the container.
For this TrueNAS Compose deployment:

```sh
sudo docker compose exec web python cli.py add wedding-2026 "Wedding 2026" wedding-2026
```

Or continue using the SMB-based Windows CLI process above. The folder argument
is relative to `/media` and must already exist. Pick a unique PIN (preferably
at least 8 random digits)
and give it to the intended recipients through a private channel. The CLI
stores only a salted scrypt hash in the data dataset. Running the command again
for the same slug resets that album's title/PIN.

The gallery URL will be `https://YOUR_HOST/g/wedding-2026`. Allow a few minutes
for the first scan and thumbnail generation. Check the worker logs if files are
missing or unsupported.

## 5. Configure HTTPS and network access

Configure a DNS hostname and HTTPS in a reverse proxy or Cloudflare Tunnel.
Forward that hostname to `http://TRUE_NAS_LAN_IP:3000` (or the host port you
selected). The browser-facing connection must be HTTPS because login cookies
are Secure and HttpOnly. Do not expose the app's HTTP port directly to the
internet and do not configure a URL path prefix such as `/gallery`; the app is
served at the domain root with gallery paths like `/g/<slug>`.

The default app trusts no forwarded client-IP headers. If operating exclusively
behind a trusted reverse proxy, set `PROXY_HOPS` to the number of trusted proxy
hops in the `web` service so per-IP PIN throttling sees the original client
address. Only do this if direct public access to the app port is blocked; a
client that can reach the app directly could otherwise forge forwarded headers.

For private LAN-only testing, you may set `COOKIE_SECURE=false` and visit
`http://TRUE_NAS_LAN_IP:3000/g/<slug>`. Do not use that setting for a
public/internet-facing deployment.

## 6. Verify

- `https://YOUR_HOST/healthz` returns `{"ok":true}`.
- `https://YOUR_HOST/g/wedding-2026` loads the PIN screen.
- After login, the title/count appear and images load.
- Test a favorite, a full-size preview, a video if present, and an original download.
- Download the gallery ZIP and verify it contains the original media files.
- Sign in as administrator and confirm a photo deletion removes the original file.
- In TrueNAS, check both service logs and confirm the worker reports processed
  files without permission or codec errors.

## Updating

Push the updated project to GitHub and wait for the image publishing workflow to
finish. In the TrueNAS Apps UI, update/redeploy the app to pull the new image.
For Compose deployments, run:

```sh
sudo docker compose pull
sudo docker compose up -d
```

Keep the data dataset when upgrading: it contains album PIN hashes and
favorites. The cache can be rebuilt by the worker if removed; the worker checks
for missing cached files and regenerates them.

## Supported media and limitations

The worker handles common JPEG/PNG/WebP/TIFF/HEIC/AVIF images, several RAW
formats with embedded JPEG previews, and MP4/MOV/MKV/M4V/AVI video using
ExifTool and ffmpeg. RAW files without an embedded JPEG preview are skipped.
Initial video conversion and large libraries can use substantial CPU, storage,
and time.

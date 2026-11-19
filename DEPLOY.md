# Deployment

Scribefire's site is a **static export**. `npm run build` produces an `out/`
folder of plain HTML/CSS/JS that any host can serve — no Node.js process has
to run in production.

```
app/, components/, lib/  →  npm run build  →  out/  →  your server
```

---

## Where the code lives

| What | Where |
|---|---|
| Source of truth | `github.com/ashishgupta4tech/scribefire` (branch `main`) |
| Local clone | `C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW\scribefire-repo` |
| Built site (local) | `C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW\scribefire-site` |
| Built site (server) | the `DEPLOY_PATH` directory on your server |
| Public URL | whatever domain you point at that server |

Nothing is generated at runtime. The server only holds the finished `out/`
folder.

---

## Everyday workflow

```bash
cd "C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW\scribefire-repo"

# 1. make your change
# 2. verify it locally
npm install
npm run build
npx serve out          # or: python preview.py

# 3. commit and push
git add -A
git commit -m "what changed"
git push origin main
```

**The push is the deploy.** The GitHub Actions workflow
(`.github/workflows/deploy.yml`) rebuilds the site and uploads it to the
server automatically. There is no manual step and no `vercel`/`rsync` command
to remember.

If the workflow is not configured yet (no `DEPLOY_HOST` variable), the build
still runs and the artifact is kept in the Actions tab — so nothing breaks
while you set the server up.

---

## One-time server setup

This is for a plain Linux server (Ubuntu/Debian) with nginx. Skip it if you
would rather use Vercel or Cloudflare Pages (see the end of this file).

### 1. Point a domain at the server

Add an **A record** for your domain in your DNS provider:

| Name | Type | Value |
|---|---|---|
| `scribefire` (or `@` for the root) | A | your server's IP |

Wait for DNS to propagate, then confirm:

```bash
nslookup scribefire.com
```

### 2. Create a deploy user on the server

```bash
sudo adduser --disabled-password deploy
sudo mkdir -p /home/deploy/.ssh
sudo cp ~/.ssh/id_ed25519.pub /home/deploy/.ssh/authorized_keys
sudo chown -R deploy:deploy /home/deploy/.ssh
sudo chmod 700 /home/deploy/.ssh
sudo chmod 600 /home/deploy/.ssh/authorized_keys

# allow the deploy user to write the site and reload nginx
echo 'deploy ALL=(ALL) NOPASSWD: /bin/systemctl reload nginx' \
  | sudo tee /etc/sudoers.d/deploy-reload
sudo chmod 440 /etc/sudoers.d/deploy-reload
```

### 3. Create the site directory and the nginx server block

```bash
sudo mkdir -p /var/www/scribefire
sudo chown -R deploy:deploy /var/www/scribefire
```

`/etc/nginx/sites-available/scribefire`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name scribefire.com www.scribefire.com;

    root /var/www/scribefire;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }

    # hashed build assets are safe to cache forever
    location /_next/static/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # the entry HTML must always be revalidated
    location = /index.html {
        add_header Cache-Control "no-cache";
    }

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;
}
```

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/scribefire /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> HTTPS: `sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx -d scribefire.com -d www.scribefire.com`
> adds the certificate and redirects HTTP to HTTPS automatically.

### 4. Add the GitHub secrets

In the repository, go to **Settings → Secrets and variables → Actions**:

**Variables** (tab "Variables"):

| Name | Value |
|---|---|
| `DEPLOY_HOST` | your server's IP or hostname |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_PATH` | `/var/www/scribefire` |

**Secrets** (tab "Secrets"):

| Name | Value |
|---|---|
| `DEPLOY_SSH_KEY` | the private key for the `deploy` user |

To create that key on your machine:

```bash
ssh-keygen -t ed25519 -C "scribefire-deploy" -f ~/.ssh/scribefire_deploy
cat ~/.ssh/scribefire_deploy          # paste this into DEPLOY_SSH_KEY
ssh-copy-id -i ~/.ssh/scribefire_deploy.pub deploy@your-server-ip
```

### 5. Push and watch it work

```bash
git push origin main
```

Open the repository's **Actions** tab — the "Deploy site" run shows the build
and the upload. When it finishes, `https://your-domain` serves the new build.

---

## Deploying by hand

If you ever need to publish without waiting for Actions:

```bash
npm run build
rsync -az --delete out/ deploy@your-server-ip:/var/www/scribefire/
ssh deploy@your-server-ip "sudo systemctl reload nginx"
```

---

## Other hosts

Because the output is static, these all work with no code changes:

- **Vercel** — `npx vercel --prod`. Import the GitHub repo instead and every
  push deploys automatically. The current site already runs this way.
- **Cloudflare Pages** — connect the repo, build command `npm run build`,
  output directory `out`.
- **Netlify / GitHub Pages / S3+CloudFront / nginx** — all just need the
  `out/` folder.

The one thing a host must support is the `/_next/static/` path — do not
rewrite it, only the page routes.

---

## Domain notes

`getlikhari.vercel.app` is the old address and still points at the previous
deployment. To move the domain:

- **Vercel** — in the project, add the custom domain under *Settings →
  Domains*, then update the DNS records it shows.
- **Your own server** — follow the nginx setup above; the DNS A record is all
  that is needed.

---

## The desktop app

The app is in the same repository under `scribefire/`. The site's download
button points at the repository root, which is why both live together:

```
lib/site.ts   →   export const REPO = "https://github.com/ashishgupta4tech/scribefire"
```

That is the single place the URL lives — change it there and every download
button on the site follows.

To ship a new release, tag the repo and attach the build:

```bash
git tag v1.0.0
git push origin v1.0.0
```

then create the release on GitHub and upload `scribefire.zip`.

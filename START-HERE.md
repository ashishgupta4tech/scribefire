# Starting fresh

Everything lives in the GitHub repository. The local folders are just a
working copy — if you delete them, nothing is lost.

```
github.com/ashishgupta4tech/scribefire   <- the source of truth
```

---

## What is where

| Path in the repo | What it is |
|---|---|
| `app/`, `components/`, `lib/`, `public/`, `types/` | the website (Next.js) |
| `scribefire/` | the desktop app (Python) |
| `tests/` | the app's test suites |
| `docs/` | the UI review page and the HUD fixture |
| `DEPLOY.md` | how the site gets deployed |
| `.github/workflows/deploy.yml` | the build that runs on every push |

The website's built output (`out/`) is **not** in the repo — it is generated
by the build on Vercel, so there is no point in storing it.

---

## Getting a working copy again

```bash
git clone https://github.com/ashishgupta4tech/scribefire.git
cd scribefire
```

That is all you need for the website. For the desktop app you also need its
dependencies:

```bash
python -m pip install -r requirements.txt
python -m scribefire --check     # verifies the install
```

---

## Making a change

```bash
# edit whatever you like, then:
git add -A
git commit -m "what you changed"
git push origin main
```

**That push is the whole deploy.** Within about half a minute the site is
updated at <https://scribefire.vercel.app>. There is no build to run and no
file to upload by hand — Vercel picks the commit up and publishes it.

You can also edit files directly on GitHub in the browser and commit there;
that triggers the same deploy.

---

## Running the site locally

```bash
npm install
npm run dev          # development server
```

or build and serve the static export the way Vercel does:

```bash
npm run build        # -> out/
npx serve out
```

---

## Running the app locally

```bash
python -m scribefire             # normal start
python -m scribefire --no-tray   # without the tray icon
python -m scribefire --check     # environment report
```

The app stores its state outside the repo, so it survives a re-clone:

| OS | Config | Log |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Scribefire\config.json` | `%LOCALAPPDATA%\Scribefire\Logs\scribefire.log` |
| macOS | `~/Library/Application Support/Scribefire/config.json` | `~/Library/Logs/Scribefire/scribefire.log` |

Your API key lives in the OS keychain under the service `scribefire` — you
never need to put it in a file, and it is not in the repository.

---

## Running the tests

```bash
python tests/scribefire_smoke.py
python tests/scribefire_hud_test.py
python tests/scribefire_inject_test.py
python tests/scribefire_pipeline_test.py
python tests/scribefire_ui_test.py
python tests/scribefire_models_test.py
```

---

## Before you push

If you changed the website, check it locally first:

```bash
npm run build
```

A failed build is a failed deploy, and Vercel will show the error in the
**Deployments** tab of the project.

---

## If the push asks for a username

That means Git could not find a saved credential. On Windows:

```powershell
git config --global credential.helper manager
```

Then push again — Git Credential Manager opens a browser window once, you
approve it, and it remembers the result. It should not ask again.

# Run Logger

A small personal web app that loads GPS points from your [Tractive](https://tractive.com) pet tracker in a time range, turns them into a GPX file, and uploads the result to [Strava](https://www.strava.com) as a run. It is built with **Flask**, a **single HTML/CSS/JS** page (no frontend build step), and is meant to run on **Railway** from a **GitHub** repository.

## What you need

- A **GitHub** account to store the code.
- A **Railway** account (free tier is enough for light use).
- A **Tractive** account: your login email, your Tractive **app password** (the one you use in the app, not necessarily the same as the website), and your **tracker ID** (see below).
- A **Strava** account and a **Strava API application** (see below).

## 1. Get the code on GitHub

1. Create a new empty repository on GitHub (for example `run-logger`).
2. On your computer, clone it and copy the files from this project into the folder, or use **GitHub’s “Import”** / **upload files** if you prefer a web-only flow.
3. Commit and push so Railway can deploy from the repo.

## 2. Create a Strava API application

1. Open [Strava API settings](https://www.strava.com/settings/api) while logged in.
2. Create an **application** and note:
   - **Client ID**
   - **Client secret**
3. **Authorization Callback Domain**: for production, use your Railway hostname only the **domain** part, e.g. `your-app.up.railway.app` (no `https://`, no path). Strava also wants the full callback URL in your app’s settings in some places; the full URL you will use in this project is:
   - `https://YOUR_RAILWAY_DOMAIN.up.railway.app/auth/callback`  
   Put that same URL in the environment variable `STRAVA_REDIRECT_URI` (see below). For local testing you can add `127.0.0.1` and use `http://127.0.0.1:5000/auth/callback` as a second callback if Strava allows multiple entries (or use a tunnel).

## 3. Deploy on Railway

1. In [Railway](https://railway.app), create a **New Project** and connect your **GitHub** repository.
2. Railway will detect a **web** service. The included `Procfile` runs: `web: python app.py`.
3. In the service **Variables** tab, add every variable from [`.env.example`](.env.example) with real values (see the next section).
4. After deploy, open your public URL. You should see a **Sign in** page (see **App password** below), then the main form after you sign in.

## 4. Environment variables (copy from `.env.example`)

- **`FLASK_SECRET_KEY`**: a long random string (used to protect session cookies). You can run `openssl rand -hex 32` in a terminal to create one.
- **`ACCESS_PASSWORD`**: the password **you** use to open the app in the browser. Only people who know it can use your hosted URL.
- **Tractive**: `TRACTIVE_EMAIL`, `TRACTIVE_PASSWORD`, `TRACTIVE_TRACKER_ID`
- **Strava**: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REDIRECT_URI`  
- **Strava tokens** (see OAuth below): `STRAVA_ACCESS_TOKEN`, `STRAVA_REFRESH_TOKEN`, `STRAVA_TOKEN_EXPIRES_AT` (can be `0` at first; they are refreshed as you use the app).

**Do not commit a real `.env` file to Git**—only use Railway’s dashboard (or a local `.env` for development).

## 5. Connect Strava (first-time OAuth)

1. After deploy, set `STRAVA_REDIRECT_URI` exactly to:  
   `https://<your-railway-public-domain>/auth/callback`
2. In a browser, open:  
   `https://<your-railway-public-domain>/auth`  
3. Approve the Strava access request. You should see a short success message.  
4. The server saves refreshed tokens under **`/tmp/strava_tokens.json`**. If Strava returns a new **refresh token** after a refresh, copy the values from the server (or re-run OAuth) and update the variables in **Railway**, because **environment variables** do not update themselves when the file changes.

**Scopes** requested by the app include reading and writing activities so uploads and small edits are allowed.

## 6. Find your Tractive tracker ID

- You need the **numeric or UUID** identifier for the **tracker** device, not the pet’s name. Common approaches:
  - In the Tractive **web** or **app** product flows, the tracker often appears in URLs or in device details (copy the id shown for the device).
  - You can also search community notes for *Tractive graph API tracker id*; the value is what the unofficial API path uses:  
    `.../3/tracker/{TRACTIVE_TRACKER_ID}/positions`
- Put that value in `TRACTIVE_TRACKER_ID` with no extra spaces.

Use the **app password** Tractive shows for your account in the app settings if the API expects a “platform” password, not a random one-off session password.

## 7. First successful run (walkthrough)

1. **Sign in** to your site with `ACCESS_PASSWORD`.
2. **Pick a time range** that you know the animal had the tracker on and was moving (start and end, date and time in your local browser fields).
3. **Optional**: activity name, perceived exertion (1–10), description, **commute**, and a **photo** (JPG/PNG, up to 50 MB per file in the app).
4. Click **Log Run** and wait until the success screen appears, or read the plain-language error.
5. Use **View on Strava** to open the new activity. If you see a note that **perceived exertion** could not be set by the API, the app may have added **“Felt: x/10”** to the **description** instead, as a fallback. Photo upload depends on Strava’s current API rules; a message appears if a photo could not be attached.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your keys
export $(grep -v '^#' .env | xargs)  # or set variables manually; do not run blindly if .env has spaces
python app.py
# Open http://127.0.0.1:5000
```

## Notes and limitations

- **Tractive** uses an **unofficial** public-style API. Responses can change; the app tries to parse common field names for positions.
- **Strava** file upload is **asynchronous**; the app polls the upload for up to about **10 seconds**.
- **`perceived_exertion`**: Strava’s public “update activity” model may not always return or accept this field; the app falls back to a line in the **description** when needed.
- **Session security**: the app is meant as a **personal** tool. `ACCESS_PASSWORD` and `FLASK_SECRET_KEY` should be long and private.

## License

Use and modify for personal use. Tractive and Strava are trademarks of their owners; this project is not affiliated with them.

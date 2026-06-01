# NHM Feeders — Android wrapper

A thin WebView app that opens the NHM Feeders web app
(`https://circuit-sld.onrender.com`) as an installable Android app.

It supports everything the website does, plus:
- **Photo upload from the phone camera or gallery** (for site photos on feeders)
- Login session persistence (cookies)
- Device **back button** navigates page history
- `localStorage` (saved layouts) and pinch-zoom

## Get the APK (no Android Studio needed)

The APK is built by GitHub Actions:

1. Push to `master` (or run the **Build NHM Feeders APK** workflow manually
   from the repo's **Actions** tab → *Run workflow*).
2. Open the finished run and download the **`nhm-feeders-apk`** artifact.
3. Unzip it, copy `app-debug.apk` to your phone, and tap to install
   (you may need to allow "install from unknown sources").

## Change the URL

Edit one line — `app_url` in
`app/src/main/res/values/strings.xml` — then rebuild.

## App identity

- Name: **NHM Feeders**
- Package: `com.nhm.feeders`
- Min Android: 7.0 (API 24), targets Android 14 (API 34)

This is a **debug** APK (unsigned) — fine for internal/field use. For the
Play Store you'd add a signing config and build `assembleRelease`.

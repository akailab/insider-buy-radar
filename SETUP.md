# Set up Insider Radar on your iPhone

This takes about **20 minutes, one time**. When you're done:

* GitHub runs the scan **twice every weekday**, at about 7:15 AM and 6:45 PM New York time. It's free and your computer doesn't need to be on.
* You get an **e-mail** whenever a new buy scores 70 or higher.
* The app is on your **iPhone home screen** and always shows the latest scan.

You need a free GitHub account, a Gmail account for sending alerts, and Safari on your iPhone.

---

## Step 1: Create a GitHub account and an empty project

1. Go to **github.com** and sign up (free). Your username becomes part of your app's web address.
2. Click **+** (top right), then **New repository**.
3. Set **Repository name** to `insider-buy-radar`.
4. Choose **Public**. Free GitHub Pages sites must be public. The app only shows public SEC data, and your passwords stay hidden (see Step 4).
5. Click **Create repository**.

## Step 2: Upload the app files

1. On the new repository page, click the link **uploading an existing file**.
2. Open the unzipped `insider-buy-radar` folder on your computer, select **everything inside it**, and drag it onto the GitHub page.
3. Wait for the uploads to finish, then click **Commit changes** at the bottom.

## Step 3: Add the automatic schedule (one file)

Mac and Windows hide folders whose names start with a dot, so this one file has to be created by hand.

1. In your repository, click **Add file**, then **Create new file**.
2. In the name box, type exactly: `.github/workflows/insider-radar.yml`
   (GitHub turns each `/` into a folder as you type).
3. Open `setup/insider-radar.yml` from the unzipped folder in any text editor (TextEdit or Notepad). Copy **all** of it and paste it into the big box on GitHub.
4. Click **Commit changes**, then **Commit changes** again.

If GitHub says the file already exists, it was uploaded in Step 2 and you can skip this step.

## Step 4: Create a Gmail "app password" for the alerts

Google won't let apps sign in with your normal password. Instead, you create a special 16-letter password that can only send mail.

1. Go to **myaccount.google.com**, then **Security**. Turn on **2-Step Verification** if it isn't on already. App passwords require it.
2. In the search bar at the top of your Google Account, type **App passwords** and open it.
3. Name it `Insider Radar`, click **Create**, and copy the 16-letter password shown.

## Step 5: Give GitHub your settings (stored privately, never shown)

In your repository, go to **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**. Add these four, one at a time:

| Name | Value |
|---|---|
| `SEC_CONTACT_EMAIL` | Your e-mail. The SEC requires a contact address from every app. |
| `ALERT_EMAIL_TO` | Where alerts should go (can be the same e-mail) |
| `SMTP_USER` | Your Gmail address |
| `SMTP_PASSWORD` | The 16-letter app password from Step 4 |

**Optional:** to change the alert threshold, open the **Variables** tab on the same page and add `ALERT_MIN_SCORE` with a value such as `60`.

## Step 6: Turn on the website

1. Go to **Settings**, then **Pages**.
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.

## Step 7: Run it the first time

1. Click the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **Insider Radar** on the left, then **Run workflow**. Leave it on **scan** and click the green **Run workflow** button.
3. **The first run takes about 1 to 1½ hours.** It downloads 30 days of filings plus 4 years of insider history. Later runs take a few minutes.
4. When it shows a green check, your app is live at:
   **`https://YOUR-GITHUB-USERNAME.github.io/insider-buy-radar/`**
5. Optional: run it once more and choose **backtest**. This fills in the Backtest tab and takes 1 to 2 hours. After that it runs by itself on the 1st of every month.

## Step 8: Put it on your iPhone

1. Open the link from Step 7 in **Safari**. It must be Safari; other browsers can't add home-screen apps on iPhone.
2. Tap the **Share** button (the square with an arrow pointing up).
3. Scroll down, tap **Add to Home Screen**, then tap **Add**.
4. Open **Insider Radar** from your home screen. It runs full-screen like a normal app. Tap the circular arrow at the top to refresh.

---

## Everyday use

* **Buys tab:** tap any buy to see why it scored what it did. Use **Filters** to change roles, minimum size, score or date range.
* **Congress tab:** stock trades disclosed by members of Congress in the last 90 days, with filters for chamber, party, buy/sell, amount, leaders, committee oversight overlap and "C-suite buying too". It's a data feed only: no score and no alerts.
* **Backtest tab:** shows whether high scores actually beat the S&P 500 in the past, and which factors mattered most.
* **Changing the scoring:** edit `config/weights.json` in GitHub by opening the file and clicking the pencil icon. The next scan uses the new weights.
* **Run a scan right now:** go to **Actions**, then **Insider Radar**, then **Run workflow**.

## Updating to a new version

Already set up? You don't need to redo the steps above. Your secrets, Gmail password and website settings all stay. Three steps:

1. **Upload the new files:** in your repository, click **Add file**, then **Upload files**. Drag in everything from the new unzipped folder and click **Commit changes**. Files with the same name are replaced.
2. **Update the schedule file:** open `.github/workflows/insider-radar.yml` in your repository and click the **pencil** icon. Select all the text, delete it, paste in **all** of `setup/insider-radar.yml` from the new folder, and click **Commit changes**. This step matters for the Congress tab: the new schedule installs the PDF reader that House filings need.
3. **Run it once:** go to **Actions**, then **Insider Radar**, then **Run workflow** (leave it on **scan**). The Congress tab fills in when the run finishes. On your iPhone, open the app and tap the refresh arrow.

**New app icon:** iPhones keep the icon an app had when you first added it. To get the new icon, press and hold Insider Radar on your home screen, tap **Remove App**, then **Delete from Home Screen**. Then add it again from Safari (Step 8). Your filters are kept.

**Desktop mode:** just double-click the Start file as usual. It installs the PDF reader automatically the first time.

## If something goes wrong

* **A red ✗ in the Actions tab:** click the run and open the step with the red mark to see the error.
  * *"Set SEC_CONTACT_EMAIL"*: the secret from Step 5 is missing or misspelled.
  * *"SEC refused the request (403)"*: the SEC is limiting traffic. It usually works on the next run.
  * *"Username and Password not accepted"*: `SMTP_USER` or `SMTP_PASSWORD` is wrong. Create a new app password (Step 4) and update the secret.
* **"e-mail isn't configured" in the log:** one of the three e-mail secrets is missing.
* **Congress tab says House or Senate "couldn't be refreshed":** that site was unreachable on the last run, so the app shows the last saved data. The Senate site sometimes blocks cloud servers. Running the desktop app from home usually works.
* **No price data (no 52-week high or "since trade" numbers):** Yahoo Finance sometimes blocks requests from cloud servers. Everything else still works, and prices usually come back on a later run.
* **The website shows a 404:** check Step 6, and wait for the first run to finish with a green check.
* **The schedule stopped:** GitHub pauses schedules in repositories with no activity for 60 days. The app saves a small status file after every run to prevent this. If it does happen, click **Enable workflow** in the Actions tab.

*For research and education. This is not investment advice.*

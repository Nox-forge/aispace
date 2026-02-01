const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

(async () => {
    const browser = await chromium.launch({
        headless: false,
        args: ['--no-sandbox']
    });
    const page = await browser.newPage();

    console.log('Navigating to GitHub signup...');
    await page.goto('https://github.com/signup', { waitUntil: 'networkidle' });

    // Take screenshot of initial page
    await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup_1.png' });
    console.log('Screenshot 1 saved');

    // Wait a moment for page to fully load
    await page.waitForTimeout(2000);

    // GitHub signup is a multi-step form
    // Step 1: Email
    try {
        const emailInput = await page.waitForSelector('input[id="email"]', { timeout: 10000 });
        if (emailInput) {
            await emailInput.fill('clawdbot-ai@tutamail.com');
            console.log('Email entered');
            await page.waitForTimeout(1000);
            await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup_2_email.png' });

            // Click continue
            const continueBtn = await page.waitForSelector('button[data-continue-to="password-container"]', { timeout: 5000 });
            if (continueBtn) await continueBtn.click();
            await page.waitForTimeout(2000);
        }
    } catch (e) {
        console.log('Email step issue:', e.message);
    }

    // Step 2: Password
    try {
        const pwInput = await page.waitForSelector('input[id="password"]', { timeout: 10000 });
        if (pwInput) {
            await pwInput.fill('NoxAi2026!SecureGH');
            console.log('Password entered');
            await page.waitForTimeout(1000);

            const continueBtn = await page.waitForSelector('button[data-continue-to="username-container"]', { timeout: 5000 });
            if (continueBtn) await continueBtn.click();
            await page.waitForTimeout(2000);
        }
    } catch (e) {
        console.log('Password step issue:', e.message);
    }

    // Step 3: Username
    try {
        const userInput = await page.waitForSelector('input[id="login"]', { timeout: 10000 });
        if (userInput) {
            await userInput.fill('nox-ai');
            console.log('Username entered');
            await page.waitForTimeout(2000);
            await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup_3_username.png' });

            const continueBtn = await page.waitForSelector('button[data-continue-to="opt-in-container"]', { timeout: 5000 });
            if (continueBtn) await continueBtn.click();
            await page.waitForTimeout(2000);
        }
    } catch (e) {
        console.log('Username step issue:', e.message);
    }

    // Step 4: Email preferences
    try {
        const optIn = await page.waitForSelector('input[id="opt_in"]', { timeout: 5000 });
        if (optIn) {
            // Don't opt in to emails
            console.log('Email preferences step');
        }
        const continueBtn = await page.waitForSelector('button[data-continue-to="captcha-and-submit-container"]', { timeout: 5000 });
        if (continueBtn) await continueBtn.click();
        await page.waitForTimeout(3000);
    } catch (e) {
        console.log('Opt-in step issue:', e.message);
    }

    // Final screenshot - likely CAPTCHA
    await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup_4_captcha.png', fullPage: true });
    console.log('Final screenshot saved - check for CAPTCHA');

    // Wait for manual intervention or timeout
    console.log('Waiting 30 seconds for any manual steps...');
    await page.waitForTimeout(30000);

    await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup_5_final.png', fullPage: true });
    console.log('Done');

    await browser.close();
})();

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
    await page.waitForTimeout(3000);

    // Fill the single-page form
    // Email
    const emailInput = await page.$('input[id="email"]');
    if (emailInput) {
        await emailInput.click();
        await emailInput.fill('clawdbot-ai@tutamail.com');
        console.log('Email filled');
        await page.waitForTimeout(1500);
    }

    // Password
    const pwInput = await page.$('input[id="password"]');
    if (pwInput) {
        await pwInput.click();
        await pwInput.fill('NoxAi2026!SecureGH');
        console.log('Password filled');
        await page.waitForTimeout(1500);
    }

    // Username
    const userInput = await page.$('input[id="login"]');
    if (userInput) {
        await userInput.click();
        await userInput.fill('nox-ai');
        console.log('Username filled');
        await page.waitForTimeout(3000);  // Wait for availability check
    }

    await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup2_filled.png' });
    console.log('Form filled screenshot saved');

    // Check if username is available (look for error message)
    const usernameError = await page.$('.form-control.is-errored');
    if (usernameError) {
        console.log('Username might have an error - check screenshot');
    }

    // Click Create account
    try {
        const createBtn = await page.waitForSelector('button:has-text("Create account")', { timeout: 5000 });
        if (createBtn) {
            console.log('Clicking Create account...');
            await createBtn.click();
            await page.waitForTimeout(5000);
        }
    } catch (e) {
        console.log('Create button issue:', e.message);
        // Try alternative selector
        try {
            await page.click('button[type="submit"]');
            console.log('Clicked submit button');
            await page.waitForTimeout(5000);
        } catch (e2) {
            console.log('Submit button also failed:', e2.message);
        }
    }

    await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup2_after_submit.png', fullPage: true });
    console.log('After submit screenshot saved');

    // Wait for potential CAPTCHA or email verification page
    await page.waitForTimeout(15000);
    await page.screenshot({ path: '/home/clawdbot/aispace/experiments/gh_signup2_final.png', fullPage: true });
    console.log('Final screenshot saved');

    console.log('Current URL:', page.url());
    await browser.close();
})();

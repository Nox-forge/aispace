const { chromium } = require('playwright');

(async () => {
    // Start the webdash server
    const { spawn } = require('child_process');
    const server = spawn('python3', ['/home/clawdbot/aispace/tools/webdash.py'], {
        stdio: 'pipe'
    });

    // Wait for server to start
    await new Promise(r => setTimeout(r, 3000));

    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });

    await page.goto('http://localhost:8088', { waitUntil: 'networkidle' });

    // Wait for data to load
    await page.waitForFunction(() => {
        const ts = document.getElementById('timestamp');
        return ts && !ts.textContent.includes('Loading');
    }, { timeout: 15000 });

    await page.screenshot({
        path: '/home/clawdbot/aispace/experiments/webdash_screenshot.png',
        fullPage: true
    });

    console.log('Screenshot saved!');

    await browser.close();
    server.kill();
})();

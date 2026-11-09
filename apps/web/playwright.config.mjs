import { defineConfig, devices } from "@playwright/test";

// API (SUPPORT_EMAIL_SENDER=smtp -> Mailpit) and web must already be running. The voice service (apps/voice, :8002)
// is started here unless one is already up; it and the API must both run on the fake Twilio, speech to text and text
// to speech (SUPPORT_ENV=test, SUPPORT_TELEPHONY=fake, SUPPORT_VOICE_PUBLIC_URL=http://localhost:8002). The voice specs
// (voice.spec.js, voice-transfer.spec.js) need nothing else from it: the fake Realtime model is registered in test too.
export default defineConfig({
  testDir: "tests/e2e",
  testMatch: "**/*.spec.js",
  timeout: 60_000,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "uv run uvicorn supportvoice.main:create_app --factory --host 127.0.0.1 --no-proxy-headers --port 8002 --ws-max-size 16384",
    cwd: "../..",
    port: 8002,
    reuseExistingServer: true,
    timeout: 60_000,
    env: {
      ...process.env,
      SUPPORT_ENV: "test",
      SUPPORT_TELEPHONY: "fake",
      SUPPORT_VOICE_PUBLIC_URL: "http://localhost:8002",
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

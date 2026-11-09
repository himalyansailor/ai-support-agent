This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://github.com/vercel/next.js/tree/canary/packages/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.js`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## End-to-end tests

`make e2e` (or `npm run e2e` here) runs the Playwright specs in `tests/e2e` against servers you start
yourself:

1. `make up` and `make migrate`.
2. The API with `SUPPORT_EMAIL_SENDER=smtp` (invite emails go to Mailpit) and either
   `SUPPORT_JOB_QUEUE=inline` or `SUPPORT_JOB_QUEUE=celery` plus `make dev-worker` (`make dev-api`
   already passes `--ws-max-size 16384`).
3. This app on :3000 (`make dev-web`).
4. For `widget.spec.js`: `make build-widget`, the stub LLM (`node apps/web/tests/e2e/stub-llm.mjs`,
   an OpenAI-compatible server on :5555) and the demo page on :5500
   (`python3 -m http.server 5500 --directory apps/widget/demo`).

The knowledge and widget specs use the local embedder BAAI/bge-small-en-v1.5 from
`SUPPORT_MODEL_CACHE_DIR` (downloaded on first use).

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

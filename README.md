# image2ppt SDKs

Official SDKs for the [image2ppt](https://image2ppt.com) API — turn images and PDFs into **editable** PowerPoint (`.pptx`) decks.

You send a batch of images or PDF pages; image2ppt reconstructs the layout with AI into editable text and shapes, and hands you back one `.pptx`.

> This repository contains only the client SDKs, examples, and API docs. The conversion engine is a hosted service at [image2ppt.com](https://image2ppt.com).

## SDKs

| Language | Package | Install | Docs |
|---|---|---|---|
| Python | [`image2ppt`](https://pypi.org/project/image2ppt/) (PyPI) | `pip install image2ppt` | [python/README.md](./python/README.md) |
| TypeScript / Node.js | [`image2ppt`](https://www.npmjs.com/package/image2ppt) (npm) | `npm install image2ppt` | [typescript/README.md](./typescript/README.md) |

Both SDKs are **server-side** clients. Never ship your API key to a browser or mobile app — anyone can read it there. Call image2ppt from your backend.

Both also support graceful job cancellation: pages already running finish and remain
deliverable, while pages that have not started are skipped and refunded.

When a job finishes, both SDKs report the outcome **page by page** — not just how
many pages were lost, but which ones, why, and whether the page made it into the
deck at all. See each SDK's README.

Both also cover the integration side of the API:

- **Completion callbacks** — pass a callback URL and get a signed `POST` when the job
  ends; `verify_webhook()` / `verifyWebhook()` checks the signature
  ([Standard Webhooks](https://www.standardwebhooks.com)).
- **Safe resubmission** — every submission carries an `Idempotency-Key`, so a
  submission whose outcome is unknown (a dropped connection, a timeout) is resent
  without any risk of being charged twice.
- **Submit by URL** — hand over `https` links instead of uploading.
- **Page selection** — convert only some pages (`"1-3, 7"`), and pay only for those.
- **Job list** — every API job with what it charged and refunded, for reconciliation.

## Quick look

```python
from image2ppt import Image2PPTClient

client = Image2PPTClient(api_key="i2p_live_...")
job = client.convert(["slide1.png", "report.pdf"], dest_path="out.pptx")
print("done, credits used:", job.credits_used)
```

```typescript
import { Image2PPTClient } from "image2ppt";

const client = new Image2PPTClient({ apiKey: "i2p_live_..." });
const job = await client.convert(["slide1.png", "report.pdf"], "out.pptx");
console.log("done, credits used:", job.creditsUsed);
```

## More files than one request can hold

One request carries at most 90MB of file content and 50 pages (with a page selection: 50 pages selected), and one file at most 35MB. Both SDKs check all three **locally, before uploading** — going over the request limit is not a polite error, the connection is simply cut before the API can answer.

For a pile bigger than that, `convert_all()` / `convertAll()` splits it into batches and writes **one PPTX per batch** (there is no server-side merge). `convert()` is unchanged: one job, one deck. Details in each SDK's README.

## Getting an API key

1. Sign in at [image2ppt.com](https://image2ppt.com).
2. Open the **Developer / API** page from the account menu.
3. Create a key (looks like `i2p_live_xxxx`). It's shown in full **once** — save it.

API access is available to accounts that have purchased credits (it opens automatically on your first purchase). Conversion is billed per page (1 page = 1 credit), shared with the web app.

## API reference

- Full HTTP reference: <https://image2ppt.com/en/docs/api> · 中文版：<https://image2ppt.com/docs/api>
- Base URL: `https://image2ppt.com`
- Auth: `Authorization: Bearer i2p_live_...`

## Support

Found a bug or want a feature? [Open an issue](https://github.com/image2ppt/image2ppt-sdk/issues). For account, billing, or key questions, use the in-app support on [image2ppt.com](https://image2ppt.com).

## License

[MIT](./LICENSE)

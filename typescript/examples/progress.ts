/**
 * Show live progress while a conversion runs: overall percent, time left,
 * queue position, and what each page is doing right now.
 *
 *   export IMAGE2PPT_API_KEY=i2p_live_your_key
 *   npx tsx progress.ts slide1.png slide2.png
 *
 * Set IMAGE2PPT_LANGUAGE=zh-CN to get the step text in Chinese (English by
 * default). Building a progress bar in your own web page instead? Keep the API
 * key on your server: submit there, have the page ask your server every few
 * seconds, and answer it from client.getJob(jobId) — the same fields shown below.
 */

import { Image2PPTClient, Image2PPTError, type Job } from "image2ppt";

function show(job: Job, elapsedMs: number): void {
  let line = `[${Math.round(elapsedMs / 1000)}s] ${job.progress}%`;
  if (job.queueAhead) line += `, ${job.queueAhead} job(s) ahead in the queue`;
  if (job.estimatedRemainingSeconds !== null) {
    line += `, about ${job.estimatedRemainingSeconds}s left`;
  }
  console.log(line);
  for (const page of job.pageProgress ?? []) {
    // stepLabel is display text: show it as-is, never branch on it.
    console.log(
      `    page ${page.pageNumber}: ${page.progress}%  ${page.stepLabel ?? ""}`,
    );
  }
}

async function main(): Promise<number> {
  const apiKey = process.env.IMAGE2PPT_API_KEY;
  const paths = process.argv.slice(2);
  if (!apiKey || paths.length === 0) {
    console.error("set IMAGE2PPT_API_KEY and pass file paths");
    return 2;
  }

  const client = new Image2PPTClient({
    apiKey,
    acceptLanguage: process.env.IMAGE2PPT_LANGUAGE,
  });
  try {
    const job = await client.convert(paths, "out.pptx", { onProgress: show });
    console.log(`wrote out.pptx — credits used: ${job.creditsUsed}`);
    return 0;
  } catch (err) {
    if (err instanceof Image2PPTError) {
      console.error(`failed: ${err.message}`);
      return 1;
    }
    throw err;
  }
}

main().then((code) => process.exit(code));

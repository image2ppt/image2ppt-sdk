/**
 * Official Node.js / TypeScript client for the image2ppt API.
 * Convert images and PDFs into editable PowerPoint (.pptx) decks.
 *
 * ```ts
 * import { Image2PPTClient } from "image2ppt";
 *
 * const client = new Image2PPTClient({ apiKey: "i2p_live_..." });
 * const job = await client.convert(["slide.png", "report.pdf"], "out.pptx");
 * console.log("credits used:", job.creditsUsed);
 * ```
 *
 * Server-side only — keep your API key off the browser.
 * See https://github.com/image2ppt/image2ppt-sdk for docs and examples.
 */

export { DEFAULT_BASE_URL, Image2PPTClient } from "./client.js";
export { VERSION } from "./version.js";
export {
  APIConnectionError,
  APITimeoutError,
  AuthenticationError,
  IdempotencyKeyInProgressError,
  IdempotencyKeyMismatchError,
  Image2PPTError,
  Image2PPTTimeoutError,
  InsufficientCreditsError,
  InvalidAspectRatioError,
  InvalidCallbackUrlError,
  InvalidFileError,
  InvalidIdempotencyKeyError,
  InvalidPagesError,
  InvalidParameterError,
  InvalidUrlError,
  JobAlreadyFinishedError,
  JobCancelledError,
  JobFailedError,
  JobNotFoundError,
  MalformedResponseError,
  MalformedUploadError,
  NoFilesError,
  NotReadyError,
  OutputExpiredError,
  PageRateExceededError,
  PagesOutOfRangeError,
  RateLimitedError,
  ServerError,
  TooManySlidesError,
  UploadAbortedError,
  UrlFetchFailedError,
  WebhookVerificationError,
} from "./errors.js";
export type { ErrorInit } from "./errors.js";
export {
  BATCH_TARGET_BYTES,
  MAX_FILE_BYTES,
  MAX_PAGES_PER_JOB,
  MAX_UPLOAD_BYTES,
  checkFileSize,
  checkPageSelection,
  checkSubmission,
  planBatches,
} from "./limits.js";
export type { UploadItem } from "./limits.js";
export { CallbackStatus, Job, JobList, PageError, PageResult } from "./types.js";
export { WebhookEvent, verifyWebhook } from "./webhook.js";
export type { VerifyWebhookOptions, WebhookHeaders } from "./webhook.js";
export type {
  Account,
  AspectRatio,
  CancellationResult,
  ClientOptions,
  ConvertAllOptions,
  ConvertOptions,
  JobError,
  ListJobsOptions,
  JobStatus,
  Locale,
  PageStatus,
  SubmitAllOptions,
  SubmitOptions,
  WaitOptions,
} from "./types.js";

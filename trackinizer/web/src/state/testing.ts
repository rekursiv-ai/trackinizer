// Test helpers for the user's data in the preferences; only tests import this file.
import { DEFAULT_PREFERENCES, receivePreferences, sendWaiting } from "../preferences";

/**
 * End a test that may have typed a draft or changed the user's data: send what
 * waits, let the server's answers in, and put the defaults back, so the next test
 * starts as a user with nothing kept.
 */
export async function resetPreferences(): Promise<void> {
  await sendWaiting();
  receivePreferences(DEFAULT_PREFERENCES);
}

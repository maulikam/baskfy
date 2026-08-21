import { handlers } from "@/lib/auth";

/**
 * Auth.js v5's route handler — this is also the endpoint the browser API client reads the access
 * token from (`GET /api/auth/session`), so the token never has to be embedded in the HTML.
 */
export const { GET, POST } = handlers;

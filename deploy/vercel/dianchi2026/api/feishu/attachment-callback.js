const ALLOWED_RETURN_HOSTS = new Set([
  "127.0.0.1",
  "192.168.1.35",
  "192.168.1.55",
  "192.168.2.162",
]);

export default function handler(request, response) {
  if (request.method !== "GET") {
    response.status(405).send("Method not allowed");
    return;
  }

  const state = Array.isArray(request.query.state)
    ? request.query.state[0]
    : request.query.state || "";
  const code = Array.isArray(request.query.code)
    ? request.query.code[0]
    : request.query.code || "";
  const error = Array.isArray(request.query.error)
    ? request.query.error[0]
    : request.query.error || "";
  if (!state || (!code && !error)) {
    response.status(400).send("Missing Feishu OAuth result");
    return;
  }

  try {
    const payload = JSON.parse(Buffer.from(state, "base64url").toString("utf8"));
    const returnUrl = new URL(String(payload.return_url || ""));
    if (
      returnUrl.protocol !== "http:" ||
      returnUrl.port !== "6185" ||
      !ALLOWED_RETURN_HOSTS.has(returnUrl.hostname) ||
      !/^\/api\/v1\/assistant-attachments\/[A-Za-z0-9_-]+\/oauth\/callback$/.test(
        returnUrl.pathname,
      )
    ) {
      response.status(400).send("Invalid attachment return URL");
      return;
    }
    returnUrl.searchParams.set("state", state);
    if (code) returnUrl.searchParams.set("code", code);
    if (error) returnUrl.searchParams.set("error", error);
    response.redirect(302, returnUrl.toString());
  } catch {
    response.status(400).send("Invalid Feishu OAuth state");
  }
}

// WebSocket agent connection: connects, reconnects with backoff, and
// dispatches the typed events agent_chat.py sends (turn_start, text,
// tool_start, field_updated, highlight, turn_end, error, warning).
//
// Each `message` send is self-contained (carries session_id), so a
// reconnect doesn't need to restore any server-side state.

export function createWsClient(wsUrl, handlers) {
  let socket = null;
  let closedByUser = false;
  let backoffMs = 1000;
  const MAX_BACKOFF_MS = 15000;

  function connect() {
    closedByUser = false;
    socket = new WebSocket(wsUrl);

    socket.addEventListener("open", () => {
      backoffMs = 1000;
      handlers.onOpen?.();
    });

    socket.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      const fn = handlers[msg.type];
      if (fn) fn(msg);
    });

    socket.addEventListener("close", () => {
      handlers.onClose?.();
      if (!closedByUser) scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      // "close" always follows "error" on a WebSocket, so reconnection is
      // handled there — this listener only exists to avoid an unhandled
      // error surfacing in the console.
    });
  }

  function scheduleReconnect() {
    setTimeout(connect, backoffMs);
    backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
  }

  /** Raw frame. `action` selects which agent handles it — the API's
   * RouteSelectionExpression is $request.body.action, so "message" reaches the
   * filling agent and "author" reaches the guide-writing one. */
  function sendRaw(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      handlers.onSendFailed?.("not connected yet — try again in a moment");
      return false;
    }
    socket.send(JSON.stringify(payload));
    return true;
  }

  function send(sessionId, message, scopeFieldIds) {
    return sendRaw({
      action: "message",
      session_id: sessionId,
      message,
      scope_field_ids: scopeFieldIds && scopeFieldIds.length ? scopeFieldIds : undefined,
    });
  }

  function close() {
    closedByUser = true;
    socket?.close();
  }

  connect();
  return { send, sendRaw, close };
}

// Thin REST wrappers. No auth headers yet — the backend runs with the
// Cognito authorizer commented out (see lambdas/README.md's "Not done yet"),
// so every request is effectively anonymous until that's wired up.

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(apiUrl, path, options = {}) {
  const res = await fetch(`${apiUrl}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new ApiError(body.message || `request failed: ${res.status}`, res.status, body);
  }
  return body;
}

export function createApi(apiUrl) {
  return {
    createSession(filename, contentType) {
      return request(apiUrl, "/sessions", {
        method: "POST",
        body: JSON.stringify({ filename, content_type: contentType }),
      });
    },
    getSession(sid) {
      return request(apiUrl, `/sessions/${encodeURIComponent(sid)}`);
    },
    setFields(sid, updates) {
      return request(apiUrl, `/sessions/${encodeURIComponent(sid)}/fields`, {
        method: "PATCH",
        body: JSON.stringify({ updates }),
      });
    },
    /** Move field boxes. Separate from setFields because this rewrites the
     * schema, not values — see lambdas/functions/api_set_schema.py. */
    setSchema(sid, updates) {
      return request(apiUrl, `/sessions/${encodeURIComponent(sid)}/schema`, {
        method: "PATCH",
        body: JSON.stringify({ updates }),
      });
    },
    validate(sid) {
      return request(apiUrl, `/sessions/${encodeURIComponent(sid)}/validate`, { method: "POST" });
    },
    render(sid, { strict = true, flatten = false } = {}) {
      return request(apiUrl, `/sessions/${encodeURIComponent(sid)}/render`, {
        method: "POST",
        body: JSON.stringify({ strict, flatten }),
      });
    },

    // ------------------------------------------------------------- catalog
    // The closed list of forms. Picking one skips upload and ingest entirely.

    listCatalog({ includeDrafts = false } = {}) {
      return request(apiUrl, `/catalog${includeDrafts ? "?include_drafts=1" : ""}`);
    },
    getCatalogEntry(cid, { includeFields = false } = {}) {
      const q = includeFields ? "?include_fields=1" : "";
      return request(apiUrl, `/catalog/${encodeURIComponent(cid)}${q}`);
    },
    createCatalogEntry(sessionId, meta) {
      return request(apiUrl, "/catalog", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, ...meta }),
      });
    },
    updateCatalogEntry(cid, changes) {
      return request(apiUrl, `/catalog/${encodeURIComponent(cid)}`, {
        method: "PATCH",
        body: JSON.stringify(changes),
      });
    },
    catalogSourceUrl(cid, filename, contentType) {
      return request(apiUrl, `/catalog/${encodeURIComponent(cid)}/sources`, {
        method: "POST",
        body: JSON.stringify({ filename, content_type: contentType }),
      });
    },
    /** Rebuild a catalog entry's boxes by re-reading its stored master.
     * Returns a session id to poll; nothing is written to the entry until you
     * adopt it below. The catalog path never consults the schema registry, so
     * this is the only way an entry defined by an older pipeline gets fixed. */
    reingestCatalogEntry(cid) {
      return request(apiUrl, `/catalog/${encodeURIComponent(cid)}/reingest`, { method: "POST" });
    },
    /** Publish a rebuilt schema into the entry. Keeps guide.md and any boxes a
     * person placed by hand. */
    adoptSchemaFromSession(cid, sessionId) {
      return request(apiUrl, `/catalog/${encodeURIComponent(cid)}`, {
        method: "PATCH",
        body: JSON.stringify({ adopt_schema_from_session: sessionId }),
      });
    },
    startFromCatalog(cid) {
      return request(apiUrl, `/catalog/${encodeURIComponent(cid)}/sessions`, { method: "POST" });
    },
  };
}

/** Direct S3 PUT — the presigned URL is signed for a specific Content-Type,
 * so this header must match exactly what /sessions was asked for. */
export async function uploadToS3(uploadUrl, file, contentType) {
  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": contentType },
    body: file,
  });
  if (!res.ok) throw new ApiError(`upload failed: ${res.status}`, res.status, {});
}

export { ApiError };

import { CreateJobsResponse, ErrorResponse, HealthResponse, Job } from "@pdf-ocr/shared";
import { z } from "zod";

/**
 * OpenAPI 3.1 document. Component schemas are derived from the same shared Zod
 * schemas the SPA uses, so the machine-readable contract can't drift from the
 * runtime types. Served at /openapi.json with Swagger UI at /docs.
 */
export function buildOpenApiDoc(): Record<string, unknown> {
  const ref = (name: string) => ({ $ref: `#/components/schemas/${name}` });
  const jsonError = {
    description: "Error",
    content: { "application/json": { schema: ref("Error") } },
  };

  return {
    openapi: "3.1.0",
    info: {
      title: "PDF OCR API",
      version: "1.0.0",
      description:
        "Self-hosted GPU OCR. Upload PDFs and convert to searchable PDF, Markdown, or Word " +
        "(PaddleOCR 3.7 / PaddleOCR-VL). Jobs are async: submit, then poll GET /jobs/{id} " +
        "(or stream GET /jobs/{id}/events) until status is 'done', then GET /download/{id}.",
    },
    servers: [{ url: "/" }],
    components: {
      securitySchemes: {
        ApiKey: { type: "apiKey", in: "header", name: "X-API-Key" },
        Bearer: { type: "http", scheme: "bearer" },
      },
      schemas: {
        Job: z.toJSONSchema(Job),
        CreateJobsResponse: z.toJSONSchema(CreateJobsResponse),
        HealthResponse: z.toJSONSchema(HealthResponse),
        Error: z.toJSONSchema(ErrorResponse),
      },
    },
    paths: {
      "/api/v1/jobs": {
        get: {
          summary: "List jobs",
          responses: {
            "200": {
              description: "Jobs",
              content: {
                "application/json": {
                  schema: {
                    type: "object",
                    properties: { jobs: { type: "array", items: ref("Job") } },
                  },
                },
              },
            },
          },
        },
        post: {
          summary: "Submit PDF(s) for conversion",
          description:
            "multipart/form-data: one or more `files` (PDF), " +
            "repeated `modes` (pdf|markdown|word), and `locale` (zh-TW|zh-CN|en). " +
            "markdown+word share one VL pass. " +
            "Returns created job ids.",
          requestBody: {
            required: true,
            content: {
              "multipart/form-data": {
                schema: {
                  type: "object",
                  properties: {
                    files: { type: "array", items: { type: "string", format: "binary" } },
                    modes: {
                      type: "array",
                      items: { type: "string", enum: ["pdf", "markdown", "word"] },
                    },
                    locale: { type: "string", enum: ["zh-TW", "zh-CN", "en"] },
                  },
                  required: ["files", "modes"],
                },
              },
            },
          },
          responses: {
            "200": {
              description: "Created",
              content: { "application/json": { schema: ref("CreateJobsResponse") } },
            },
            "400": jsonError,
            "429": jsonError,
          },
        },
      },
      "/api/v1/jobs/{id}": {
        get: {
          summary: "Get job status + result",
          parameters: [{ name: "id", in: "path", required: true, schema: { type: "string" } }],
          responses: {
            "200": { description: "Job", content: { "application/json": { schema: ref("Job") } } },
            "404": jsonError,
          },
        },
        delete: {
          summary: "Delete a job and its outputs",
          parameters: [{ name: "id", in: "path", required: true, schema: { type: "string" } }],
          responses: { "200": { description: "Deleted" }, "404": jsonError },
        },
      },
      "/api/v1/jobs/{id}/events": {
        get: {
          summary: "Stream job progress (SSE)",
          description: "text/event-stream of `job` events (the full Job object) until terminal.",
          parameters: [{ name: "id", in: "path", required: true, schema: { type: "string" } }],
          responses: { "200": { description: "SSE stream" }, "404": jsonError },
        },
      },
      "/api/v1/jobs/{id}/cancel": {
        post: {
          summary: "Cancel a job",
          parameters: [{ name: "id", in: "path", required: true, schema: { type: "string" } }],
          responses: { "200": { description: "Cancel requested" }, "404": jsonError },
        },
      },
      "/api/v1/download/{id}": {
        get: {
          summary: "Download a completed job's artifact",
          description:
            "Returns application/pdf (pdf), application/zip (markdown), or .docx (word).",
          parameters: [{ name: "id", in: "path", required: true, schema: { type: "string" } }],
          responses: { "200": { description: "File" }, "404": jsonError },
        },
      },
      "/api/v1/preview/{id}": {
        get: {
          summary: "Preview markdown output as text",
          parameters: [{ name: "id", in: "path", required: true, schema: { type: "string" } }],
          responses: {
            "200": {
              description: "Markdown",
              content: {
                "application/json": {
                  schema: {
                    type: "object",
                    properties: { content: { type: "string" }, filename: { type: "string" } },
                  },
                },
              },
            },
            "404": jsonError,
          },
        },
      },
      "/api/v1/health": {
        get: {
          summary: "Health + worker/GPU status",
          responses: {
            "200": {
              description: "Health",
              content: { "application/json": { schema: ref("HealthResponse") } },
            },
          },
        },
      },
    },
  };
}

export const SWAGGER_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>PDF OCR API</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>window.onload=()=>{window.ui=SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui'})}</script>
</body></html>`;

# Engram — SDK Quickstart (Any Language)


## Authentication — OTel Bearer Token

All apps sending telemetry must include your `OTEL_BEARER_TOKEN` in the
OTLP exporter headers. Set it via the standard OTel environment variable:

```bash
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer your-token-here"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4317"
```

Or pass it in code (example for Python):
```python
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

exporter = OTLPSpanExporter(
    endpoint="http://localhost:4317",
    headers={"Authorization": f"Bearer {os.environ['OTEL_BEARER_TOKEN']}"},
    insecure=True,
)
```

---
All you need in any language is:
1. Install the OTel SDK for your language (usually auto-instrumentation — zero code changes)
2. Set the `engram.project_id` resource attribute to your project UUID
3. Point the exporter at `localhost:4317` (or your Engram host)

That's it. No custom SDK. No HTTP calls in your app code.

---

## Python (FastAPI / Flask)

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap --action=install   # auto-detects your framework
```

```bash
# Run your app with zero code changes
opentelemetry-instrument \
  --service_name my-api \
  --exporter_otlp_endpoint http://localhost:4317 \
  --resource_attributes "engram.project_id=YOUR_PROJECT_UUID,deployment.environment=production" \
  python app.py
```

---

## Node.js / TypeScript (Express, Fastify, NestJS)

```bash
npm install @opentelemetry/auto-instrumentations-node \
            @opentelemetry/exporter-trace-otlp-grpc \
            @opentelemetry/sdk-node
```

```javascript
// tracing.js — require this BEFORE anything else
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
const { Resource } = require('@opentelemetry/resources');

const sdk = new NodeSDK({
  resource: new Resource({
    'service.name':          'my-node-api',
    'engram.project_id':    'YOUR_PROJECT_UUID',
    'deployment.environment': 'production',
  }),
  traceExporter: new OTLPTraceExporter({
    url: 'http://localhost:4317',
  }),
  instrumentations: [getNodeAutoInstrumentations()],
});

sdk.start();
```

```bash
node -r ./tracing.js server.js
```

---

## Java (Spring Boot)

```bash
# Download the OTel Java agent — no code changes needed
curl -L https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar \
  -o opentelemetry-javaagent.jar
```

```bash
# Run your JAR with the agent attached
java \
  -javaagent:./opentelemetry-javaagent.jar \
  -Dotel.service.name=my-spring-api \
  -Dotel.resource.attributes="engram.project_id=YOUR_PROJECT_UUID,deployment.environment=production" \
  -Dotel.exporter.otlp.endpoint=http://localhost:4317 \
  -jar my-app.jar
```

One flag. No code changes. Every Spring MVC endpoint, JDBC call, and Redis operation is traced automatically.

---

## Go

```bash
go get go.opentelemetry.io/otel \
        go.opentelemetry.io/otel/sdk \
        go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc
```

```go
// otel.go — call InitTracer() from main() before starting your server
package telemetry

import (
    "context"
    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    "go.opentelemetry.io/otel/resource"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
    "google.golang.org/grpc"
)

func InitTracer(ctx context.Context) (*sdktrace.TracerProvider, error) {
    exporter, err := otlptracegrpc.New(ctx,
        otlptracegrpc.WithInsecure(),
        otlptracegrpc.WithEndpoint("localhost:4317"),
        otlptracegrpc.WithDialOption(grpc.WithBlock()),
    )
    if err != nil {
        return nil, err
    }

    res := resource.NewWithAttributes(
        semconv.SchemaURL,
        semconv.ServiceName("my-go-api"),
        semconv.DeploymentEnvironment("production"),
        attribute.String("engram.project_id", "YOUR_PROJECT_UUID"),
    )

    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(exporter),
        sdktrace.WithResource(res),
    )
    otel.SetTracerProvider(tp)
    return tp, nil
}
```

---

## Ruby on Rails

```bash
gem install opentelemetry-sdk \
            opentelemetry-instrumentation-all \
            opentelemetry-exporter-otlp
```

```ruby
# config/initializers/opentelemetry.rb
require 'opentelemetry/sdk'
require 'opentelemetry/instrumentation/all'
require 'opentelemetry-exporter-otlp'

OpenTelemetry::SDK.configure do |c|
  c.resource = OpenTelemetry::SDK::Resources::Resource.create(
    'service.name'           => 'my-rails-api',
    'engram.project_id'     => 'YOUR_PROJECT_UUID',
    'deployment.environment' => 'production',
  )
  c.add_span_processor(
    OpenTelemetry::SDK::Trace::Export::BatchSpanProcessor.new(
      OpenTelemetry::Exporter::OTLP::Exporter.new(endpoint: 'http://localhost:4317')
    )
  )
  c.use_all  # auto-instruments Rails, ActiveRecord, Net::HTTP, Redis, etc.
end
```

---

## .NET / C#

```bash
dotnet add package OpenTelemetry.Exporter.OpenTelemetryProtocol
dotnet add package OpenTelemetry.Extensions.Hosting
dotnet add package OpenTelemetry.Instrumentation.AspNetCore
dotnet add package OpenTelemetry.Instrumentation.Http
```

```csharp
// Program.cs
builder.Services.AddOpenTelemetry()
    .ConfigureResource(resource => resource
        .AddService("my-dotnet-api")
        .AddAttributes(new Dictionary<string, object>
        {
            ["engram.project_id"]      = "YOUR_PROJECT_UUID",
            ["deployment.environment"]  = "production",
        }))
    .WithTracing(tracing => tracing
        .AddAspNetCoreInstrumentation()
        .AddHttpClientInstrumentation()
        .AddOtlpExporter(opts => opts.Endpoint = new Uri("http://localhost:4317")));
```

---

## The one thing every language needs

```
engram.project_id = YOUR_PROJECT_UUID
```

Get your project UUID from:
```bash
curl -s -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "my-app"}' | python3 -m json.tool
# Copy the "project_id" field
```

Your API key is also returned — keep it for the GitHub webhook and settings page.

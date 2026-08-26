import assert from "node:assert/strict";
import test from "node:test";

import { createRailwayContext, project } from "railway/iac";

import railwayConfig from "./railway.ts";

test("compiles the exact local-upload publication service", async () => {
  const context = createRailwayContext({
    command: "plan",
    environment: "production",
    projectName: "palimpsest",
  });
  const definition = await railwayConfig(context, project);

  assert.equal(definition.name, "palimpsest");
  assert.equal(definition.resources.length, 1);
  assert.deepEqual(definition.resources[0], {
    address: "service.palimpsest-publication",
    type: "service",
    kind: "empty",
    name: "palimpsest-publication",
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "ops/railway/Dockerfile.static",
    },
    deploy: {
      healthcheckPath: "/healthz",
      healthcheckTimeout: 300,
      numReplicas: 1,
      restartPolicyMaxRetries: 5,
    },
    networking: {
      customDomains: {
        "palimpsest.info": { port: 8080 },
        "www.palimpsest.info": { port: 8080 },
      },
    },
  });
});

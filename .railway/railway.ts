import { defineRailway, project, service } from "railway/iac";

export default defineRailway(() => {
  const publication = service("palimpsest-publication", {
    // No source is declared: releases continue to arrive via `railway up` from
    // the immutable local Git-archive bundle.
    build: {
      builder: "DOCKERFILE",
      dockerfilePath: "ops/railway/Dockerfile.static",
    },
    deploy: {
      healthcheckPath: "/healthz",
      healthcheckTimeout: 300,
      numReplicas: 1,
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 5,
    },
    domains: ["palimpsest.info", "www.palimpsest.info"],
  });

  return project("palimpsest", {
    resources: [publication],
  });
});

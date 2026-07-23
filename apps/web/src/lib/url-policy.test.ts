import {
  APPROVED_EXTERNAL_URL_HOSTS,
  describeUrlPolicyRejection,
  evaluateExternalUrlPolicy,
} from "./url-policy";

describe("evaluateExternalUrlPolicy", () => {
  it("allows every approved connector terms host over https with no port", () => {
    for (const host of APPROVED_EXTERNAL_URL_HOSTS) {
      const decision = evaluateExternalUrlPolicy(`https://${host}/terms`);
      expect(decision).toEqual({
        allowed: true,
        url: `https://${host}/terms`,
        host,
      });
    }
  });

  it("allows the default https port expressed explicitly", () => {
    const decision = evaluateExternalUrlPolicy(
      "https://www.crossref.org:443/terms/",
    );
    expect(decision.allowed).toBe(true);
  });

  it("rejects a missing or empty URL", () => {
    expect(evaluateExternalUrlPolicy(undefined)).toEqual({
      allowed: false,
      reason: "missing-url",
    });
    expect(evaluateExternalUrlPolicy(null)).toEqual({
      allowed: false,
      reason: "missing-url",
    });
    expect(evaluateExternalUrlPolicy("")).toEqual({
      allowed: false,
      reason: "missing-url",
    });
    expect(evaluateExternalUrlPolicy("   ")).toEqual({
      allowed: false,
      reason: "missing-url",
    });
  });

  it("rejects a URL that cannot be parsed", () => {
    expect(evaluateExternalUrlPolicy("not a url")).toEqual({
      allowed: false,
      reason: "unparseable-url",
    });
  });

  it("rejects non-https schemes", () => {
    expect(
      evaluateExternalUrlPolicy("http://www.crossref.org/terms/"),
    ).toEqual({ allowed: false, reason: "unsupported-scheme" });
    expect(
      evaluateExternalUrlPolicy("javascript:alert(1)"),
    ).toEqual({ allowed: false, reason: "unsupported-scheme" });
    expect(
      evaluateExternalUrlPolicy("ftp://www.crossref.org/terms/"),
    ).toEqual({ allowed: false, reason: "unsupported-scheme" });
  });

  it("rejects URLs with embedded credentials", () => {
    expect(
      evaluateExternalUrlPolicy("https://user:pass@www.crossref.org/terms/"),
    ).toEqual({ allowed: false, reason: "embedded-credentials" });
    expect(
      evaluateExternalUrlPolicy("https://user@www.crossref.org/terms/"),
    ).toEqual({ allowed: false, reason: "embedded-credentials" });
  });

  it("rejects unsafe non-standard ports", () => {
    expect(
      evaluateExternalUrlPolicy("https://www.crossref.org:8443/terms/"),
    ).toEqual({ allowed: false, reason: "unsafe-port" });
    expect(
      evaluateExternalUrlPolicy("https://www.crossref.org:80/terms/"),
    ).toEqual({ allowed: false, reason: "unsafe-port" });
  });

  it("rejects loopback, private, and link-local hosts", () => {
    const blocked = [
      "https://localhost/terms",
      "https://127.0.0.1/terms",
      "https://0.0.0.0/terms",
      "https://[::1]/terms",
      "https://10.1.2.3/terms",
      "https://192.168.1.1/terms",
      "https://169.254.1.1/terms",
      "https://172.16.0.5/terms",
      "https://172.31.255.255/terms",
      "https://intranet.local/terms",
      "https://gateway.internal/terms",
      "https://box.localhost/terms",
    ];
    for (const url of blocked) {
      expect(evaluateExternalUrlPolicy(url)).toEqual({
        allowed: false,
        reason: "private-or-local-host",
      });
    }
  });

  it("rejects bare hostnames with no dot", () => {
    expect(evaluateExternalUrlPolicy("https://gateway/terms")).toEqual({
      allowed: false,
      reason: "private-or-local-host",
    });
  });

  it("does not treat 172.32.x.x (outside the private range) or a plain public host as private", () => {
    // 172.32.0.1 is outside the 172.16.0.0/12 private range but is not an
    // approved host, so it is rejected for that reason instead.
    expect(evaluateExternalUrlPolicy("https://172.32.0.1/terms")).toEqual({
      allowed: false,
      reason: "unapproved-host",
    });
  });

  it("rejects hosts that are not on the approved allowlist", () => {
    expect(
      evaluateExternalUrlPolicy("https://evil.example.com/terms"),
    ).toEqual({ allowed: false, reason: "unapproved-host" });
    expect(
      evaluateExternalUrlPolicy("https://www.crossref.org.evil.com/terms"),
    ).toEqual({ allowed: false, reason: "unapproved-host" });
  });

  it("is case-insensitive when matching the host allowlist", () => {
    const decision = evaluateExternalUrlPolicy(
      "https://WWW.CROSSREF.ORG/terms/",
    );
    expect(decision).toEqual({
      allowed: true,
      url: "https://www.crossref.org/terms/",
      host: "www.crossref.org",
    });
  });
});

describe("describeUrlPolicyRejection", () => {
  it("returns a distinct, human-readable message for every rejection reason", () => {
    const reasons = [
      "missing-url",
      "unparseable-url",
      "unsupported-scheme",
      "embedded-credentials",
      "unsafe-port",
      "private-or-local-host",
      "unapproved-host",
    ] as const;
    const messages = reasons.map((reason) => describeUrlPolicyRejection(reason));
    expect(new Set(messages).size).toBe(reasons.length);
    for (const message of messages) {
      expect(message.length).toBeGreaterThan(10);
    }
  });
});

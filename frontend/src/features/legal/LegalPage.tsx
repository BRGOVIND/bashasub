export function LegalPage() {
  return <main className="interior-page legal-page">
    <div className="eyebrow">Legal & privacy</div>
    <div className="interior-heading"><div><h1>Clear boundaries, before launch.</h1><p>This development preview is not a public hosted service. These notes describe the current frontend and identify what must be settled before publication.</p></div></div>
    <div className="section-bar"><span>Current notices</span><span>Public launch review pending</span></div>
    <div className="legal-sections">
      <section id="privacy"><h2>Privacy note</h2><p>This frontend does not install analytics, cookies, or a service worker. Text or Markdown idea files are read locally into the draft; their contents are sent to the Redstone API only when you submit an agent request. Project names and prompts also go to the API when submitted. Hosting logs and backend data handling depend on a future deployment and are not defined here.</p><p>A publishable privacy notice still needs the operator identity and contact channel, hosting/provider recipients, retention periods, and applicable jurisdiction. Do not treat this note as a completed privacy policy.</p></section>
      <section id="terms"><h2>Service terms</h2><p>No public account, payment, or hosted-service terms are offered by this development build. Access to the source code is governed by the repository's Apache License 2.0. Hosted-service terms, if needed, must be drafted for the actual operator and deployment before launch.</p></section>
      <section id="contact"><h2>Contact & source</h2><p>Maintainer on GitHub: <a href="https://github.com/BRGOVIND" target="_blank" rel="noopener noreferrer">@BRGOVIND</a>. Questions and bug reports: <a href="https://github.com/BRGOVIND/bashasub/issues" target="_blank" rel="noopener noreferrer">repository issues</a>. No public email or legal operator name has been supplied.</p><p>Source and license: <a href="https://github.com/BRGOVIND/bashasub" target="_blank" rel="noopener noreferrer">BRGOVIND/bashasub</a> · <a href="https://github.com/BRGOVIND/bashasub/blob/main/LICENSE" target="_blank" rel="noopener noreferrer">Apache-2.0 license text</a>.</p></section>
    </div>
  </main>
}

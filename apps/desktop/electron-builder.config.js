const extraResources = [
  {
    from: "../../dist/BiliSum",
    to: "backend/BiliSum"
  },
  process.platform === "win32"
    ? {
        from: "../../apps/desktop/build/icon.ico",
        to: "icon.ico"
      }
    : null
].filter(Boolean);

module.exports = {
  appId: "com.bilisum.desktop",
  productName: "BiliSum",
  artifactName: "${productName}-${version}-${os}-${arch}-Setup.${ext}",
  directories: {
    output: "../../dist/desktop"
  },
  files: [
    "dist-electron/**/*",
    "announcement.md"
  ],
  extraResources,
  mac: {
    target: ["dmg", "zip"],
    artifactName: "${productName}-${version}-${os}-${arch}.${ext}",
    icon: "../../apps/desktop/build/icon.icns",
    category: "public.app-category.productivity",
    hardenedRuntime: false,
    gatekeeperAssess: false
  },
  dmg: {
    sign: false,
    artifactName: "${productName}-${version}-${os}-${arch}.${ext}"
  },
  win: {
    target: [
      {
        target: "nsis",
        arch: ["x64"]
      }
    ],
    icon: "../../apps/desktop/build/icon.ico"
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true,
    allowElevation: true,
    createDesktopShortcut: true,
    perMachine: false,
    shortcutName: "BiliSum",
    uninstallDisplayName: "BiliSum",
    installerIcon: "../../apps/desktop/build/icon.ico",
    uninstallerIcon: "../../apps/desktop/build/icon.ico"
  },
  publish: {
    provider: "github",
    owner: "lycohana",
    repo: "BiliSum",
    releaseType: "release"
  }
};

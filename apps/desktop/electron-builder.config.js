const iconIcoPath = "../../apps/desktop/build/icon.ico";
const extraResources = [
  {
    from: "../../dist/VidMind",
    to: "backend/VidMind"
  },
  {
    from: iconIcoPath,
    to: "icon.ico"
  }
];

module.exports = {
  appId: "com.vidmind.desktop",
  productName: "VidMind",
  artifactName: "${productName}-${version}-${os}-${arch}-Setup.${ext}",
  directories: {
    output: "../../dist/desktop"
  },
  files: [
    "dist-electron/**/*",
    "announcement.md"
  ],
  extraResources,
  win: {
    "target": [
      {
        "target": "nsis",
        "arch": [
          "x64"
        ]
      }
    ],
    "icon": iconIcoPath,
    "sign": null,
    "signAndEditExecutable": true,
    "signDlls": false,
    "requestedExecutionLevel": "asInvoker",
    "fileAssociations": []
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true,
    allowElevation: true,
    createDesktopShortcut: true,
    perMachine: false,
    shortcutName: "VidMind",
    uninstallDisplayName: "VidMind",
    installerIcon: iconIcoPath,
    uninstallerIcon: iconIcoPath
  },
  publish: {
    provider: "github",
    owner: "qianshengli",
    repo: "vidmind",
    releaseType: "release"
  },
  afterSign: null
};

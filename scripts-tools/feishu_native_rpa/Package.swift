// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "FeishuRPA",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "FeishuRPA",
            path: "Sources/FeishuRPA"
        ),
    ]
)

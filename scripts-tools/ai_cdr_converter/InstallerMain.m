#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

@interface InstallerDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSProgressIndicator *progress;
@end

@implementation InstallerDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    self.window = [[NSWindow alloc]
        initWithContentRect:NSMakeRect(0, 0, 440, 180)
                  styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = @"AI→CDR 后台工具";
    [self.window center];

    NSTextField *label = [NSTextField labelWithString:@"正在安装到 mini4，请稍候……"];
    label.alignment = NSTextAlignmentCenter;
    label.font = [NSFont systemFontOfSize:16 weight:NSFontWeightMedium];
    label.frame = NSMakeRect(30, 95, 380, 30);

    self.progress = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(70, 58, 300, 18)];
    self.progress.style = NSProgressIndicatorStyleBar;
    self.progress.indeterminate = YES;
    [self.progress startAnimation:nil];

    [self.window.contentView addSubview:label];
    [self.window.contentView addSubview:self.progress];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];

    NSURL *payload = [[NSBundle mainBundle].resourceURL
        URLByAppendingPathComponent:@"installer_payload.sh"];
    NSTask *task = [[NSTask alloc] init];
    NSPipe *output = [NSPipe pipe];
    task.executableURL = [NSURL fileURLWithPath:@"/bin/zsh"];
    task.arguments = @[ payload.path ];
    task.standardOutput = output;
    task.standardError = output;

    NSMutableDictionary<NSString *, NSString *> *environment =
        [NSProcessInfo.processInfo.environment mutableCopy];
    environment[@"AI_CDR_BUNDLE_ROOT"] = NSBundle.mainBundle.bundleURL.path;
    task.environment = environment;

    __weak InstallerDelegate *weakSelf = self;
    task.terminationHandler = ^(NSTask *finishedTask) {
        NSData *data = [output.fileHandleForReading readDataToEndOfFile];
        NSString *detail = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        dispatch_async(dispatch_get_main_queue(), ^{
            InstallerDelegate *strongSelf = weakSelf;
            [strongSelf.progress stopAnimation:nil];
            NSAlert *alert = [[NSAlert alloc] init];
            if (finishedTask.terminationStatus == 0) {
                alert.messageText = @"安装完成";
                alert.informativeText = @"后台服务已经启动。请在打开的“辅助功能”页面允许 AI-CDR-Converter；这是首次配置的最后一步。";
            } else {
                alert.alertStyle = NSAlertStyleCritical;
                alert.messageText = @"安装失败";
                alert.informativeText = [NSString stringWithFormat:
                    @"%@\n\n错误日志已写入 NAS 的 AI转CDR共享/installer.log。",
                    detail.length > 0 ? detail : @"安装器没有返回详细信息"];
            }
            [alert addButtonWithTitle:@"关闭"];
            [alert runModal];
            [NSApp terminate:nil];
        });
    };

    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        [self.progress stopAnimation:nil];
        NSAlert *alert = [[NSAlert alloc] init];
        alert.alertStyle = NSAlertStyleCritical;
        alert.messageText = @"安装器无法启动";
        alert.informativeText = error.localizedDescription;
        [alert addButtonWithTitle:@"关闭"];
        [alert runModal];
        [NSApp terminate:nil];
    }
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        InstallerDelegate *delegate = [[InstallerDelegate alloc] init];
        application.delegate = delegate;
        [application setActivationPolicy:NSApplicationActivationPolicyRegular];
        [application run];
    }
    return 0;
}

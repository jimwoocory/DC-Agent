.PHONY: build release clean install test

BINARY := bin/FeishuRPA
RELEASE := .build/release/FeishuRPA

build:
	swift build

release:
	swift build -c release
	@mkdir -p bin
	cp $(RELEASE) $(BINARY)
	@echo "✓ Binary at $(BINARY)"

clean:
	swift package clean
	rm -rf .build $(BINARY)

install: release
	@echo "✓ Installed to $(BINARY)"

test: release
	$(BINARY) --dry-run --verbose

dump-ax: release
	$(BINARY) --dump-ax-tree

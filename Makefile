IMAGE ?= rwx-blast-radius
SAMPLE = $(CURDIR)/.work/sample/sample
PLATFORM ?= linux/amd64
RUN_FLAGS = --rm --platform $(PLATFORM) --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp --mount "type=bind,source=$(SAMPLE)/commits,target=/data/commits,readonly" --mount "type=bind,source=$(SAMPLE)/tests.json,target=/data/tests.json,readonly"

.PHONY: build sample
build:
	docker build --platform $(PLATFORM) -t $(IMAGE) .

.work/sample/score.py:
	mkdir -p .work/sample
	curl -fLsS https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz -o .work/starter.tar.gz
	tar -xzf .work/starter.tar.gz -C .work/sample

sample: .work/sample/score.py build
	docker run $(RUN_FLAGS) $(IMAGE) > .work/output.jsonl
	python3 .work/sample/score.py "$(SAMPLE)" "$(SAMPLE)/answers.json" .work/output.jsonl

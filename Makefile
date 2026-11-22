PYTHON ?= $(if $(wildcard .local/venv/bin/python),.local/venv/bin/python,python3)
ARROW_DIR ?= $(shell $(PYTHON) -c "import importlib.util; print(importlib.util.find_spec('pyarrow').submodule_search_locations[0])")
BUILD ?= .local
CXXFLAGS ?= -O3 -DNDEBUG
CPPFLAGS += -isystem "$(ARROW_DIR)/include"
SOURCES := main.cpp events.cpp ordered_events.cpp inputs.cpp aggregate.cpp output.cpp
OBJECTS := $(patsubst %.cpp,$(BUILD)/%.o,$(SOURCES))
ifeq ($(shell uname -s),Darwin)
LIB_SUFFIX := .2100.dylib
else
LIB_SUFFIX := .so.2100
endif
ARROW_LIB := $(ARROW_DIR)/libarrow$(LIB_SUFFIX)
PARQUET_LIB := $(ARROW_DIR)/libparquet$(LIB_SUFFIX)
STARTER ?= .local/starter/starter-duckdb
DEV ?= .local/dev
ANSWER ?= .local/answer/dev-answer

.PHONY: all fixtures check-dev
all: $(BUILD)/shop

$(BUILD)/shop: $(OBJECTS)
	$(CXX) $(CXXFLAGS) -pthread $^ "$(PARQUET_LIB)" "$(ARROW_LIB)" -Wl,-rpath,"$(ARROW_DIR)" -o "$@"

$(BUILD)/%.o: %.cpp
	@mkdir -p "$(BUILD)"
	$(CXX) -std=c++17 $(CPPFLAGS) $(CXXFLAGS) -pthread -MMD -MP -c "$<" -o "$@"

fixtures: $(BUILD)/shop
	$(PYTHON) "$(STARTER)/scripts/run_fixtures.py" "$(STARTER)/fixtures" -- "$<"

check-dev: $(BUILD)/shop
	"$<" "$(DEV)" "$(BUILD)/dev-output"
	$(PYTHON) "$(STARTER)/scripts/compare.py" "$(BUILD)/dev-output" "$(ANSWER)"

-include $(OBJECTS:.o=.d)

CHANNEL ?= unpublished
CHARM := calico

setup-env:
	bash script/bootstrap

charm: setup-env
	bash script/build

upload:
ifndef NAMESPACE
	$(error NAMESPACE is not set)
endif

	env CHARM=$(CHARM) NAMESPACE=$(NAMESPACE) CHANNEL=$(CHANNEL) bash script/upload

.phony: charm upload setup-env
all: charm

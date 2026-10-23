.PHONY: help up down restart build logs ps shell fmt test clean

help:            ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up:              ## 构建并后台启动全部容器
	docker compose up -d --build

down:            ## 停止并移除容器
	docker compose down

restart:         ## 重启
	docker compose restart

build:           ## 只构建镜像
	docker compose build

logs:            ## 跟踪全部日志
	docker compose logs -f --tail=100

ps:              ## 容器状态
	docker compose ps

shell:           ## 进入 web 容器
	docker compose exec web bash

fmt:             ## 格式化（ruff）
	ruff format src probes

test:            ## 跑测试
	python -m pytest -q

dev-web:         ## 本地起看板（不经 Docker）
	PYTHONPATH=src python -m uvicorn rscbot.web.app:app --host 0.0.0.0 --port 8001 --reload

dev-collector:   ## 本地起采集器
	PYTHONPATH=src python -m rscbot.collector

clean:           ## 删除构建产物与缓存
	rm -rf data/*.db data/*.db-* .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +

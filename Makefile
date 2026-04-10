.PHONY: deploy clean status logs simulate-failure recover

# Deploy everything
deploy:
	chmod +x deploy.sh
	./deploy.sh

# Show cluster status
status:
	@export KUBECONFIG=kubeconfig-kind && \
	echo "=== Demo App ===" && \
	kubectl get all -n demo && \
	echo "" && echo "=== ArgoCD ===" && \
	kubectl get app -n argocd 2>/dev/null || true && \
	echo "" && echo "=== Agent Logs (last 10) ===" && \
	kubectl logs deployment/supk8s-agent -n demo --tail=10 2>/dev/null || echo "Agent not running yet"

# Watch agent logs
logs:
	export KUBECONFIG=kubeconfig-kind && kubectl logs -f deployment/supk8s-agent -n demo

# Simulate failure via GitOps: commit broken image → ArgoCD deploys it → agent detects → rollback
#
# REQUIRES: a fork you can push to. Set ARGOCD_REPO_URL in config.env to your fork
# BEFORE running `make deploy`, otherwise this target will fail at `git push` because
# you don't have write access to the upstream repo. ArgoCD must pull from the same
# repo you push to — that's the whole point of the GitOps flow.
#
# IMPORTANT: ALWAYS run `make recover` after this. Otherwise k8s/base/demo-app.yaml
# stays pinned to demo-app:v2-crash in the repo, and the next `make deploy` will
# time out at "Waiting for demo app..." because it tries to bring up crashing pods.
# If that happens: run `make recover`, then `make deploy` again.
simulate-failure:
	@echo "" && \
	echo "=== Simulating bad deployment via GitOps ===" && \
	echo "" && \
	echo "1. Changing demo-app image to v2-crash in k8s/base/demo-app.yaml..." && \
	sed -i 's|image: demo-app:v1|image: demo-app:v2-crash|' k8s/base/demo-app.yaml && \
	echo "2. Committing and pushing to GitHub..." && \
	git add k8s/base/demo-app.yaml && \
	git commit -m "Deploy v2-crash (intentional failure for demo)" && \
	git push && \
	echo "" && \
	echo "3. ArgoCD will detect the change and deploy v2-crash (~3 min)." && \
	echo "   Pods will enter CrashLoopBackOff." && \
	echo "   The supK8s agent will detect and rollback." && \
	echo "" && \
	echo "   Watch ArgoCD: http://localhost:30443 (admin / supk8s-admin)" && \
	echo "   Watch agent:  make logs" && \
	echo "" && \
	echo "Streaming agent logs (Ctrl+C to stop):" && \
	echo "" && \
	export KUBECONFIG=kubeconfig-kind && \
	kubectl logs -f deployment/supk8s-agent -n demo

# Recover: commit healthy image back
recover:
	@echo "Reverting to healthy v1..." && \
	sed -i 's|image: demo-app:v2-crash|image: demo-app:v1|' k8s/base/demo-app.yaml && \
	git add k8s/base/demo-app.yaml && \
	git commit -m "Recover: revert to demo-app:v1" && \
	git push && \
	echo "Done. ArgoCD will sync back to v1."

# Destroy everything
clean:
	kind delete cluster --name supk8s
	rm -f kubeconfig-kind kind-config.yaml

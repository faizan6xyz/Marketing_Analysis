minikube delete --purge
echo "deleting minikube profile " 
podman rmi -af
echo "deleting images " 
podman volume rm -af
echo "deleting volumes " 
podman network prune -f
echo "deleting networks" 
echo "deleting images " 
podman volume rm -af
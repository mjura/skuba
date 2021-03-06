/*
 * Copyright (c) 2019 SUSE LLC.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

package kubernetes

import (
	clientset "k8s.io/client-go/kubernetes"
	kubeconfigutil "k8s.io/kubernetes/cmd/kubeadm/app/util/kubeconfig"

	"github.com/SUSE/skuba/pkg/skuba"
	"github.com/pkg/errors"
)

func GetAdminClientSet() (*clientset.Clientset, error) {
	client, err := kubeconfigutil.ClientSetFromFile(skuba.KubeConfigAdminFile())
	if err != nil {
		return nil, errors.Wrap(err, "could not load admin kubeconfig file")
	}
	return client, nil
}

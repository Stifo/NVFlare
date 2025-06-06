# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Optional

from nvflare.edge.web.models.base_model import BaseModel


class SelectionResponse(BaseModel):
    def __init__(
        self,
        status: str,
        job_id: str = None,
        task_id: str = None,
        selection: Optional[dict] = None,
        **kwargs,
    ):
        super().__init__()
        self.status = status
        self.job_id = job_id
        self.task_id = task_id
        self.selection = selection

        if kwargs:
            self.update(kwargs)

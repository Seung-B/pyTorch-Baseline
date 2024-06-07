import torch
import torch.optim as optim
from adabelief_pytorch import AdaBelief

def select_optimizer(optimizer_name, model, learning_rate, momentum=0.9, betas=(0.9, 0.999)):
    if optimizer_name == 'SGD':
        return optim.SGD(model.parameters(), lr=learning_rate)
    elif optimizer_name == 'momentum':
        return optim.SGD(model.parameters(), lr=learning_rate, momentum=momentum)
    elif optimizer_name == 'Adam':
        return optim.Adam(model.parameters(), lr=learning_rate, betas=betas)
    elif optimizer_name == 'AdamW':
        return optim.AdamW(model.parameters(), lr=learning_rate, betas=betas)
    elif optimizer_name == 'AdaBelief':
        return AdaBelief(model.parameters(), lr=learning_rate, betas=betas)
    elif optimizer_name == 'SAM':
        base_optimizer = optim.SGD
        return SAM(model.parameters(), base_optimizer, lr=learning_rate, momentum=momentum)
    elif optimizer_name == 'SAM_Adam':
        base_optimizer = optim.Adam
        return SAM(model.parameters(), base_optimizer, lr=learning_rate, betas=betas)
    else:
        raise ValueError("Unsupported optimizer")


class SAM(torch.optim.Optimizer):
    def __init__(self, params, base_optimizer, rho=0.05, adaptive=False, **kwargs):
        assert rho >= 0.0, f"Invalid rho, should be non-negative: {rho}"

        defaults = dict(rho=rho, adaptive=adaptive, **kwargs)
        super(SAM, self).__init__(params, defaults)

        self.base_optimizer = base_optimizer(self.param_groups, **kwargs)
        self.param_groups = self.base_optimizer.param_groups
        self.defaults.update(self.base_optimizer.defaults)

    @torch.no_grad()
    def first_step(self, zero_grad=False):
        grad_norm = self._grad_norm()
        for group in self.param_groups:
            scale = group["rho"] / (grad_norm + 1e-12)

            for p in group["params"]:
                if p.grad is None: continue
                self.state[p]["old_p"] = p.data.clone()
                e_w = (torch.pow(p, 2) if group["adaptive"] else 1.0) * p.grad * scale.to(p)
                p.add_(e_w)  # climb to the local maximum "w + e(w)"

        if zero_grad: self.zero_grad()

    @torch.no_grad()
    def second_step(self, zero_grad=False):
        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None: continue
                p.data = self.state[p]["old_p"]  # get back to "w" from "w + e(w)"

        self.base_optimizer.step()  # do the actual "sharpness-aware" update

        if zero_grad: self.zero_grad()

    @torch.no_grad()
    def step(self, closure=None):
        assert closure is not None, "Sharpness Aware Minimization requires closure, but it was not provided"
        closure = torch.enable_grad()(closure)  # the closure should do a full forward-backward pass

        self.first_step(zero_grad=True)
        closure()
        self.second_step()

    def _grad_norm(self):
        shared_device = self.param_groups[0]["params"][0].device  # put everything on the same device, in case of model parallelism
        norm = torch.norm(
                    torch.stack([
                        ((torch.abs(p) if group["adaptive"] else 1.0) * p.grad).norm(p=2).to(shared_device)
                        for group in self.param_groups for p in group["params"]
                        if p.grad is not None
                    ]),
                    p=2
               )
        return norm

    def load_state_dict(self, state_dict):
        super().load_state_dict(state_dict)
        self.base_optimizer.param_groups = self.param_groups
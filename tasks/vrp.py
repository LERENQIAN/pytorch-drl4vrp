"""Defines the main task for the VRP.

The VRP is defined by the following traits:
    1. Each city has a demand in [1, 9], which must be serviced by the vehicle
    2. Each vehicle has a capacity (depends on problem), the must visit all cities
    3. When the vehicle load is 0, it __must__ return to the depot to refill
"""

import numpy as np
import torch
from torch.utils.data import Dataset
from torch.autograd import Variable
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


class VehicleRoutingDataset(Dataset):
    def __init__(self, num_samples, input_size, max_load=20, max_demand=9, seed=1234):
        super(VehicleRoutingDataset, self).__init__()

        if max_load < max_demand:
            raise ValueError(':param max_load: must be > max_demand')

        torch.manual_seed(seed)

        self.num_samples = num_samples
        self.max_load = max_load
        self.max_demand = max_demand

        # Driver location will be the first node in each
        locations = torch.rand((num_samples, 2, input_size + 1))
        locations[:, :, 0] = 0.
        self.static = locations

        # Vehicle needs a load > 0, which gets broadcasted to all states
        dynamic_shape = (num_samples, 1, input_size + 1)
        loads = torch.full(dynamic_shape, max_load / float(max_load))

        # Nodes are assigned a random demand in [1, max_demand)
        demands = torch.randint(1, max_demand + 1, dynamic_shape)
        demands = demands / float(max_load)
        demands[:, 0, 0] = 0  # depot starts with a demand of 0
        self.dynamic = torch.tensor(np.concatenate((loads, demands), axis=1))

    def __len__(self):
        return self.num_samples

    def __getitem__(self, idx):
        # (static, dynamic, start_loc)
        return (self.static[idx], self.dynamic[idx], self.static[idx, :, 0:1])

    def update_mask(self, mask, dynamic, chosen_idx=None):
        """Updates the mask used to hide non-valid states.

        Note that all math is done using integers to avoid float errors

        Parameters
        ----------
        dynamic: torch.autograd.Variable of size (1, num_feats, seq_len)
        """

        '''
        if dynamic.is_cuda:
            depot_mask = torch.cuda.FloatTensor(1, mask.size(1)).fill_(0)
        else:
            depot_mask = torch.FloatTensor(1, mask.size(1)).fill_(0)
        '''

        dint = (self.max_load * dynamic.data).int()

        # Convert floating point to integers for calculations
        loads = dint[:, 0]  # (batch_size, seq_len)
        demands = dint[:, 1]  # (batch_size, seq_len)

        # If there is no positive demand left, we can end the tour.
        # Note that the first node is the depot, which always has a negative demand
        if demands[:, 1:].eq(0).all():
            return demands * 0.

        # Otherwise, we can choose to go anywhere where demand is > 0
        new_mask = demands.ne(0)

        # We should avoid traveling to the depot back-to-back 
        repeat_home = chosen_idx.ne(0)
        if repeat_home.any():
            new_mask[repeat_home, 0] = 1.
        if (1 - repeat_home).any():
            new_mask[1 - repeat_home, 0] = 0.

        # ... unless we're waiting for all other samples in a minibatch to finish
        has_no_load = loads[:, 0].eq(0).float()
        has_no_demand = demands[:, 1:].sum(1).eq(0).float()

        combined = (has_no_load + has_no_demand).gt(0)
        if combined.any():
            new_mask[combined, 0] = 1.
            new_mask[combined, 1:] = 0.

        return new_mask.float()

    def update_dynamic(self, dynamic, chosen_idx):
        """Updates the (load, demand) dataset values."""

        # Update the dynamic elements differently for if we visit depot vs. a city
        visit = chosen_idx.ne(0)
        depot = chosen_idx.eq(0)

        # Clone the dynamic variable so we don't mess up graph
        tensor = dynamic.clone()
        all_loads = tensor[:, 0]
        all_demands = tensor[:, 1]

        load = torch.gather(all_loads, 1, chosen_idx.unsqueeze(1))
        demand = torch.gather(all_demands, 1, chosen_idx.unsqueeze(1))

        # Across the minibatch - if we've chosen to visit a city, try to satisfy
        # as much demand as possible
        if visit.any():

            # Do all calculations using integers
            load_int = (load * self.max_load).int().float()
            demand_int = (demand * self.max_load).int().float()

            # new_load = max(0, load - demand)
            new_load = torch.clamp(load_int - demand_int, min=0) / self.max_load

            # new_demand = max(0, demand - load)
            new_demand = torch.clamp(demand_int - load_int, min=0) / self.max_load
            new_demand = demand.masked_scatter_(visit.unsqueeze(1), new_demand.squeeze(1))

            # For the load we can just broadcase the same value to all nodes
            visit_idx = visit.nonzero().squeeze()
            all_loads[visit_idx] = new_load[visit_idx]
            all_demands.scatter_(1, chosen_idx.unsqueeze(1), new_demand)

            # Need to update the depot demand to be (new_load - 1)
            visit = visit.float()
            all_demands[:, 0] = all_demands[:, 0] * (1 - visit) + (new_load[:, 0] - 1) * visit

        # Return to depot to fill vehicle load
        if depot.any():
            all_loads[depot.ne(0).squeeze()] = 1.
            all_demands[depot.ne(0).squeeze(), 0] = 0.

        tensor = torch.cat((all_loads.unsqueeze(1), all_demands.unsqueeze(1)), 1)
        return torch.tensor(tensor.data, device=dynamic.device, requires_grad=True)


def reward(static, tour_indices):
    """
    Euclidean distance between all cities / nodes given by tour_indices
    """

    tour_len = []

    for i in range(static.size(0)):

        # Convert the indices back into a tour
        idx = tour_indices[i].unsqueeze(0).unsqueeze(1).expand(-1, static.size(1), -1)
        tour = torch.gather(static[i:i + 1].data, 2, idx).permute(0, 2, 1)

        # Ensure we're always returning to the depot - note the extra concat
        # won't add any extra loss, as the euclidean disistance between consecutive
        # points is 0
        start = static.data[i:i + 1, :, 0].unsqueeze(1)
        y = torch.cat((start, tour, start), dim=1)

        # Euclidean distance between each consecutive point
        dist = torch.sqrt(torch.sum(torch.pow(y[:, :-1] - y[:, 1:], 2), dim=2))
        tour_len.append(dist.sum(1))

    return torch.tensor(torch.cat(tour_len), device=static.device)


def render(static, tour_indices, save_path):
    """Plots the found solution."""

    plt.close('all')

    num_plots = 3 if int(np.sqrt(len(tour_indices))) >= 3 else 1

    _, axes = plt.subplots(nrows=num_plots, ncols=num_plots,
                           sharex='col', sharey='row')

    if num_plots == 1:
        axes = [[axes]]

    axes = [a for ax in axes for a in ax]

    for i, ax in enumerate(axes):

        # Convert the indices back into a tour
        idx = tour_indices[i]
        if len(idx.size()) == 1:
            idx = idx.unsqueeze(0)

        idx = idx.expand(static.size(1), -1)
        data = torch.gather(static[i].data, 1, idx).cpu().numpy()

        start = static[i, :, 0].cpu().data.numpy()
        x = np.hstack((start[0], data[0], start[0]))
        y = np.hstack((start[1], data[1], start[1]))

        # Assign each subtour a different colour & label in order traveled
        idx = np.hstack((0, tour_indices[i].cpu().numpy().flatten(), 0))
        where = np.where(idx == 0)[0]

        for j in range(len(where) - 1):

            low = where[j]
            high = where[j + 1]

            if low + 1 == high:
                continue

            ax.plot(x[low: high + 1], y[low: high + 1], zorder=1, label=j)

        ax.legend(loc="upper right", fontsize=3, framealpha=0.5)
        ax.scatter(x, y, s=4, c='r', zorder=2)
        ax.scatter(x[0], y[0], s=20, c='k', marker='*', zorder=3)

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

    plt.tight_layout()
    plt.savefig(save_path, bbox_inches='tight', dpi=400)


'''
def render(static, tour_indices, save_path):
    """Plots the found solution."""

    path = 'C:/Users/Matt/Documents/ffmpeg-3.4.2-win64-static/bin/ffmpeg.exe'
    plt.rcParams['animation.ffmpeg_path'] = path

    plt.close('all')

    num_plots = min(int(np.sqrt(len(tour_indices))), 3)
    fig, axes = plt.subplots(nrows=num_plots, ncols=num_plots,
                             sharex='col', sharey='row')
    axes = [a for ax in axes for a in ax]

    all_lines = []
    all_tours = []
    for i, ax in enumerate(axes):

        # Convert the indices back into a tour
        idx = tour_indices[i]
        if len(idx.size()) == 1:
            idx = idx.unsqueeze(0)

        idx = idx.expand(static.size(1), -1)
        data = torch.gather(static[i].data, 1, idx).cpu().numpy()

        start = static[i, :, 0].cpu().data.numpy()
        x = np.hstack((start[0], data[0], start[0]))
        y = np.hstack((start[1], data[1], start[1]))

        cur_tour = np.vstack((x, y))

        all_tours.append(cur_tour)
        all_lines.append(ax.plot([], [])[0])

        ax.scatter(x, y, s=4, c='r', zorder=2)
        ax.scatter(x[0], y[0], s=20, c='k', marker='*', zorder=3)

    from matplotlib.animation import FuncAnimation

    tours = all_tours

    def update(idx):

        for i, line in enumerate(all_lines):

            if idx >= tours[i].shape[1]:
                continue

            data = tours[i][:, idx]

            xy_data = line.get_xydata()
            xy_data = np.vstack((xy_data, np.atleast_2d(data)))

            line.set_data(xy_data[:, 0], xy_data[:, 1])
            line.set_linewidth(0.75)

        return all_lines

    anim = FuncAnimation(fig, update, init_func=None,
                         frames=100, interval=200, blit=False,
                         repeat=False)

    anim.save('line.mp4', dpi=160)
    plt.show()

    import sys
    sys.exit(1)
'''

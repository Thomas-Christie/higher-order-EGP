import functools
import argparse
import numpy as np
import os
import logging
import torch
import torch.optim as optim
from tqdm import tqdm
from torch_geometric.loader import DataLoader

from models.gnn import GNN
from exp import expander_graph_generation

### importing OGB
from ogb.graphproppred import PygGraphPropPredDataset, Evaluator

### importing utils
from models.utils import str2bool, set_seed

cls_criterion = torch.nn.BCEWithLogitsLoss()
reg_criterion = torch.nn.MSELoss()


def train(model, device, loader, optimizer, task_type):
    model.train()

    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        batch = batch.to(device)

        if batch.x.shape[0] == 1 or batch.batch[-1] == 0:
            pass
        else:
            pred = model(batch)
            optimizer.zero_grad()
            ## ignore nan targets (unlabeled) when computing training loss.
            is_labeled = batch.y == batch.y
            if "classification" in task_type:
                loss = cls_criterion(pred.to(torch.float32)[is_labeled], batch.y.to(torch.float32)[is_labeled])
            else:
                loss = reg_criterion(pred.to(torch.float32)[is_labeled], batch.y.to(torch.float32)[is_labeled])
            loss.backward()
            optimizer.step()


def eval(model, device, loader, evaluator):
    model.eval()
    y_true = []
    y_pred = []

    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        batch = batch.to(device)

        if batch.x.shape[0] == 1:
            pass
        else:
            with torch.no_grad():
                pred = model(batch)

            y_true.append(batch.y.view(pred.shape).detach().cpu())
            y_pred.append(pred.detach().cpu())

    y_true = torch.cat(y_true, dim=0).numpy()
    y_pred = torch.cat(y_pred, dim=0).numpy()

    input_dict = {"y_true": y_true, "y_pred": y_pred}

    return evaluator.eval(input_dict)


def main():
    # Training settings
    parser = argparse.ArgumentParser(description='GNN baselines on ogbgmol* data with Pytorch Geometrics')
    parser.add_argument('--seed', type=int, default=1,
                        help='random seed for training')
    parser.add_argument('--device', type=int, default=0,
                        help='which gpu to use if any (default: 0)')
    parser.add_argument('--gnn', type=str, default='gcn',
                        help='GNN gin or gcn, (default: gcn)')
    parser.add_argument('--drop_ratio', type=float, default=0.5,
                        help='dropout ratio (default: 0.5)')
    parser.add_argument('--num_layer', type=int, default=5,
                        help='number of GNN message passing layers (default: 5)')
    parser.add_argument('--emb_dim', type=int, default=300,
                        help='dimensionality of hidden units in GNNs (default: 300)')
    parser.add_argument('--batch_size', type=int, default=32,
                        help='input batch size for training (default: 32)')
    parser.add_argument('--epochs', type=int, default=100,
                        help='number of epochs to train (default: 100)')
    parser.add_argument('--num_workers', type=int, default=0,
                        help='number of workers (default: 0)')
    parser.add_argument('--dataset', type=str, default="ogbg-molhiv",
                        choices=['ogbg-molhiv', 'ogbg-molpcba'],
                        help='dataset name (default: ogbg-molhiv)')
    parser.add_argument('--expander', dest='expander', type=str2bool, default=True,
                        help='whether to use expander graph propagation')
    parser.add_argument('--expander_graph_generation_method', type=str, default="ramanujan-bipartite",
                        choices=['perfect-matchings', 'ramanujan-bipartite',
                                 'perfect-matchings-shortest-path',
                                 'perfect-matchings-access-time'],
                        help='method for generating expander graph')
    parser.add_argument('--expander_graph_order', type=int, default=3,
                        help='order of hypergraph expander graph')
    # parser.add_argument('--random_seed', type=int, default=42,
    #                     help='random seed used when generating ramanujan bipartite graphs')
    parser.add_argument('--expander_edge_handling', type=str, default='masking',
                        choices=['masking', 'learn-features', 'summation', 'summation-mlp'],
                        help='method to handle expander edge nodes')
    parser.add_argument('--feature', type=str, default="full",
                        help='full feature or simple feature')
    # parser.add_argument('--save_dir', type=str, default="",
    #                     help='save_dir to output result (default: )')
    args = parser.parse_args()

    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() else torch.device("cpu")

    # Set the seed for everything
    set_seed(args.seed)

    # Set path
    path = os.path.join(os.getcwd() + f"/logs/{args.dataset}/")
    if not os.path.exists(path):
        os.makedirs(path)
    save_dir = os.path.join(path, f"{args.expander_graph_generation_method}_seed{args.seed}_")
    logging.basicConfig(level=logging.INFO,
                        handlers=[
                            logging.FileHandler(save_dir + "log.txt"),
                            logging.StreamHandler()
                        ])
    logging.info(args)
    logging.info(f'Using: {device}')
    logging.info(f"Using seed {args.seed}")
    logging.info(f"Dataset: {args.dataset}")
    logging.info(f"Expander generation method: {args.expander_graph_generation_method}")
    logging.info(f"Expander graph order: {args.expander_graph_order}")
    logging.info(f"Expander edge handling: {args.expander_edge_handling}")


    expander_graph_generation_fn = None
    if args.expander_graph_generation_method == "perfect-matchings":
        expander_graph_generation_fn = functools.partial(expander_graph_generation.add_expander_edges_via_perfect_matchings,
                                                         args.expander_graph_order,
                                                         "mol")
    elif args.expander_graph_generation_method == "perfect-matchings-shortest-path":
        expander_graph_generation_fn = functools.partial(expander_graph_generation.add_expander_edges_via_perfect_matchings_shortest_paths_heuristics,
                                                         args.expander_graph_order,
                                                         "mol")
    elif args.expander_graph_generation_method == "perfect-matchings-access-time":
        expander_graph_generation_fn = functools.partial(expander_graph_generation.add_expander_edges_via_perfect_matchings_access_time_heuristics,
                                                         args.expander_graph_order,
                                                         "mol")
    elif args.expander_graph_generation_method == "ramanujan-bipartite":
        expander_graph_generation_fn = functools.partial(expander_graph_generation.add_expander_edges_via_ramanujan_bipartite_graph,
                                                         args.expander_graph_order,
                                                         "mol")

    ### automatic dataloading and splitting
    if not args.expander:
        dataset = PygGraphPropPredDataset(name=args.dataset)
    else:
        dataset = PygGraphPropPredDataset(name=args.dataset, pre_transform=expander_graph_generation_fn)

    if args.feature == 'full':
        pass
    elif args.feature == 'simple':
        logging.info('using simple feature')
        # only retain the top two node/edge features
        dataset.data.x = dataset.data.x[:, :2]
        dataset.data.edge_attr = dataset.data.edge_attr[:, :2]

    split_idx = dataset.get_idx_split()

    ### automatic evaluator. takes dataset name as input
    evaluator = Evaluator(args.dataset)

    train_loader = DataLoader(dataset[split_idx["train"]], batch_size=args.batch_size, shuffle=True,
                              num_workers=args.num_workers)
    valid_loader = DataLoader(dataset[split_idx["valid"]], batch_size=args.batch_size, shuffle=False,
                              num_workers=args.num_workers)
    test_loader = DataLoader(dataset[split_idx["test"]], batch_size=args.batch_size, shuffle=False,
                             num_workers=args.num_workers)

    if args.gnn == 'gin':
        model = GNN(gnn_type='gin', task="mol", num_class=dataset.num_tasks, num_layer=args.num_layer, emb_dim=args.emb_dim,
                    drop_ratio=args.drop_ratio, expander=args.expander, expander_edge_handling=args.expander_edge_handling).to(device)
    elif args.gnn == 'gcn':
        model = GNN(gnn_type='gcn', task="mol", num_class=dataset.num_tasks, num_layer=args.num_layer, emb_dim=args.emb_dim,
                    drop_ratio=args.drop_ratio, expander=args.expander, expander_edge_handling=args.expander_edge_handling).to(device)
    else:
        raise ValueError('Invalid GNN type')

    optimizer = optim.Adam(model.parameters(), lr=0.001)

    valid_curve = []
    test_curve = []
    train_curve = []

    best_val_so_far = 0
    for epoch in range(1, args.epochs + 1):
        logging.info("=====Epoch {}".format(epoch))
        logging.info('Training...')
        train(model, device, train_loader, optimizer, dataset.task_type)

        logging.info('Evaluating...')
        train_perf = eval(model, device, train_loader, evaluator)
        valid_perf = eval(model, device, valid_loader, evaluator)
        test_perf = eval(model, device, test_loader, evaluator)

        logging.info({'Train': train_perf, 'Validation': valid_perf, 'Test': test_perf})

        train_curve.append(train_perf[dataset.eval_metric])
        valid_curve.append(valid_perf[dataset.eval_metric])
        test_curve.append(test_perf[dataset.eval_metric])
        if 'classification' in dataset.task_type and valid_perf[dataset.eval_metric] > best_val_so_far:
            torch.save(model.state_dict(), save_dir + "best_val_model.pt")
            best_val_so_far = valid_perf[dataset.eval_metric]

    if 'classification' in dataset.task_type:
        best_val_epoch = np.argmax(np.array(valid_curve))
        best_train = max(train_curve)
    else:
        best_val_epoch = np.argmin(np.array(valid_curve))
        best_train = min(train_curve)

    logging.info('Finished training!')
    logging.info('Best validation score: {}'.format(valid_curve[best_val_epoch]))
    logging.info('Test score: {}'.format(test_curve[best_val_epoch]))

    if not save_dir == '':
        torch.save({'Val': valid_curve[best_val_epoch], 'Test': test_curve[best_val_epoch],
                    'Train': train_curve[best_val_epoch], 'BestTrain': best_train}, save_dir + "_best")
        torch.save({'Val': valid_curve, 'Test': test_curve, 'Train': train_curve}, save_dir + "_curves")
        torch.save(model.state_dict(), save_dir + "final_model.pt")


if __name__ == "__main__":
    # import pdb; pdb.set_trace()
    main()
